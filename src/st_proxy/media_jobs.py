"""Service-owned image jobs, independent of MCP connection lifetimes."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from aiohttp import ClientSession

from .config import BrokerConfig
from .coordinator import HandoffCoordinator, ImageOutcome, ImageTicket
from .errors import QueueFull
from .http import child_url
from .reference_images import ReferenceImage, ReferenceImages
from .workflows import Workflow, WorkflowCatalog

LOG = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 64 * 1024**2
MAX_IMAGES = 64
MIME_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


class JobError(ValueError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


@dataclass(kw_only=True)
class ImageResult:
    parameters: dict[str, Any]
    status: str = "preparing"
    images: list[dict[str, str]] = field(default_factory=list)
    error: dict[str, str] | None = None

    def result(self) -> dict[str, Any]:
        return copy.deepcopy({
            "status": self.status,
            "parameters": self.parameters, "images": self.images, "error": self.error,
        })


@dataclass
class ImageJob(ImageResult):
    job_id: str
    workflow_id: str
    fingerprint: str
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    def result(self) -> dict[str, Any]:
        return {"job_id": self.job_id, "workflow_id": self.workflow_id, **super().result()}


@dataclass
class BatchJob:
    job_id: str
    workflow_id: str
    fingerprint: str
    items: list[ImageResult]
    status: str = "preparing"
    error: dict[str, str] | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    def result(self) -> dict[str, Any]:
        return copy.deepcopy({
            "job_id": self.job_id, "workflow_id": self.workflow_id, "status": self.status,
            "items": [{"index": index, **item.result()} for index, item in enumerate(self.items)],
            "error": self.error,
        })


async def _disk(function, *args):
    # A cancelled waiter must not race a still-running file copy with cleanup.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def _copy_local(source: Path, destination: Path, root: Path) -> None:
    resolved = source.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise JobError("result", "resolved image is outside the configured output root")
    with resolved.open("rb") as incoming, destination.open("xb") as outgoing:
        copied = 0
        while chunk := incoming.read(1024 * 1024):
            copied += len(chunk)
            if copied > MAX_IMAGE_BYTES:
                raise JobError("result", "image exceeds the transfer limit")
            outgoing.write(chunk)
        if not copied:
            raise JobError("result", "image is empty")


def _write_chunk(path: Path, chunk: bytes, first: bool) -> None:
    with path.open("xb" if first else "ab") as stream:
        stream.write(chunk)


class MediaJobs:
    def __init__(self, config: BrokerConfig, catalog: WorkflowCatalog,
                 coordinator: HandoffCoordinator, session: ClientSession) -> None:
        self.config, self.catalog = config, catalog
        self.coordinator, self.session = coordinator, session
        self.jobs: dict[str, ImageJob | BatchJob] = {}
        self._fingerprint_key = secrets.token_bytes(32)
        self.closing = False
        self.output_dir = Path(config.mcp_output_dir).resolve()
        self.references = ReferenceImages(config, session)

    async def start(self) -> None:
        await _disk(lambda: self.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True))

    async def generate(self, job_id: str, workflow_id: str,
                       parameters: dict[str, Any],
                       references: list[str] | None = None) -> dict[str, Any]:
        return await self._generate(job_id, workflow_id, [parameters], batch=False,
                                    reference_ids=[references if references is not None else []])

    async def register_image(self, workflow_id: str, path: str) -> dict[str, Any]:
        if self.closing:
            raise JobError("shutdown", "broker is shutting down")
        return await self.references.register(self.catalog.get(workflow_id), path)

    async def generate_batch(self, job_id: str, workflow_id: str,
                             items: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(items, list) or not 1 <= len(items) <= self.config.max_queued_images:
            raise JobError("validation", "items must be non-empty and within the image queue limit")
        parameters, references = [], []
        for item in items:
            # Scalar workflow parameters cannot be objects, so the envelope is unambiguous.
            if isinstance(item, dict) and isinstance(item.get("parameters"), dict):
                if item.keys() - {"parameters", "references"}:
                    raise JobError("validation", "unknown batch item field")
                parameters.append(item["parameters"])
                references.append(item.get("references", []))
            else:
                parameters.append(item)
                references.append([])
        return await self._generate(job_id, workflow_id, parameters, batch=True,
                                    reference_ids=references)

    async def _generate(self, job_id: str, workflow_id: str,
                        items: list[dict[str, Any]], *, batch: bool,
                        reference_ids: list[list[str]]) -> dict[str, Any]:
        if self.closing:
            raise JobError("shutdown", "broker is shutting down")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
            raise JobError("validation", "job_id must be 1-128 letters, digits, _ or -")
        workflow = self.catalog.get(workflow_id)
        values = [workflow.normalize(parameters) for parameters in items]
        references = [await self.references.resolve(workflow, ids) for ids in reference_ids]
        if self.closing:
            raise JobError("shutdown", "broker is shutting down")
        fingerprint = hmac.new(self._fingerprint_key, json.dumps(
            [batch, workflow_id, values, [[r.fingerprint for r in refs] for refs in references]],
            sort_keys=True, allow_nan=False,
        ).encode(), hashlib.sha256).hexdigest()
        job = self.jobs.get(job_id)
        if job is not None:
            if not hmac.compare_digest(job.fingerprint, fingerprint):
                raise JobError("conflict", "job_id already belongs to another request")
        else:
            # Bound service-owned jobs too; the coordinator enforces the
            # authoritative shared FIFO count and byte limits afterward.
            active = sum(len(j.items) if isinstance(j, BatchJob) else 1
                         for j in self.jobs.values() if j.task is not None and not j.task.done())
            if active + len(items) > self.config.max_queued_images:
                raise JobError("queue_full", "MCP image capacity is exhausted")
            # Fingerprint the request before resolving defaults, so an omitted seed
            # attaches to the same job on retries without drawing another seed.
            for parameters in values:
                workflow.resolve_seed(parameters)
            public = [{name: value for name, value in parameters.items()
                       if not workflow.parameters[name].get("private")} for parameters in values]
            if batch:
                job = BatchJob(job_id, workflow_id, fingerprint,
                               [ImageResult(parameters=p) for p in public])
                task = self._run_batch(job, workflow, values, references)
            else:
                job = ImageJob(job_id, workflow_id, fingerprint, parameters=public[0])
                task = self._run(job, workflow, values[0], references=references[0])
            self.jobs[job_id] = job
            job.task = asyncio.create_task(task, name="mcp-image-job")
        return await self.get_job(job_id)

    async def get_job(self, job_id: str, wait: bool = True) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            raise JobError("not_found", "job is unknown in this broker process")
        if wait and job.task is not None:
            try:
                await asyncio.shield(job.task)
            except asyncio.CancelledError:
                if not (self.closing and job.task.cancelled()):
                    raise
        return job.result()

    async def _json(self, path: str, payload: dict[str, Any]) -> Any:
        async with asyncio.timeout(self.config.request_timeout):
            async with self.session.post(child_url(self.config.comfy_url, path), json=payload,
                                         allow_redirects=False,
                                         auto_decompress=True) as response:
                if response.status != 200:
                    raise JobError("upstream", "ComfyUI operation was rejected")
                return await response.json()

    async def _run_batch(self, job: BatchJob, workflow: Workflow,
                         values: list[dict[str, Any]],
                         references: list[list[ReferenceImage]]) -> None:
        tasks: list[asyncio.Task[None]] = []
        bodies: list[bytes] = []
        try:
            size = 0
            for parameters, refs in zip(values, references, strict=True):
                body = json.dumps({"prompt": workflow.build(parameters, [r.inputs for r in refs])},
                                  allow_nan=False).encode()
                if len(body) > self.config.max_workflow_body_bytes:
                    raise JobError("validation", "workflow request body exceeds the size limit")
                size += len(body)
                if size > self.config.max_queued_workflow_bytes:
                    raise JobError("queue_full", "workflow batch exceeds the queue memory limit")
                bodies.append(body)
                parameters.clear()
            del body
            job.status = "queued"
            async with self.coordinator.image_batch(bodies) as tickets:
                bodies.clear()
                # Save each result immediately, including short-lived Helto references,
                # while the existing dispatcher executes the next image in FIFO order.
                tasks = [asyncio.create_task(self._run(item, workflow, {}, ticket=ticket))
                         for item, ticket in zip(job.items, tickets, strict=True)]
                await asyncio.shield(tickets[0].submitted)
                job.status = "generating"
                await asyncio.gather(*tasks)
            if self.closing:
                raise JobError("shutdown", "broker is shutting down")
            succeeded = sum(item.status == "succeeded" for item in job.items)
            job.status = ("succeeded" if succeeded == len(job.items)
                          else "partial" if succeeded else "failed")
            if succeeded != len(job.items):
                job.error = {"category": "batch", "message": "one or more images failed"}
        except asyncio.CancelledError:
            job.status = "failed"
            job.error = {"category": "shutdown", "message": "broker is shutting down"}
        except Exception as exc:
            job.status = "failed"
            category = ("shutdown" if self.closing else "queue_full" if isinstance(exc, QueueFull)
                        else exc.category if isinstance(exc, JobError) else "submission")
            job.error = {"category": category, "message": "image batch could not complete"}
            LOG.error("MCP image batch failed: category=%s", category)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for item in job.items:
                if item.status not in {"succeeded", "failed"}:
                    item.status, item.error = "failed", copy.deepcopy(job.error)
            for parameters in values:
                parameters.clear()
            bodies.clear()

    async def _run(self, job: ImageResult, workflow: Workflow, values: dict[str, Any],
                   *, ticket: ImageTicket | None = None,
                   references: list[ReferenceImage] | None = None) -> None:
        stage = "submission"
        try:
            job.status = "queued"
            if ticket is None:
                body = json.dumps({"prompt": workflow.build(
                    values, [r.inputs for r in references or []],
                )}, allow_nan=False).encode()
                values.clear()
                completion: asyncio.Future[ImageOutcome] = (
                    asyncio.get_running_loop().create_future()
                )
                await self.coordinator.submit_image(path="/prompt", query_string="",
                                                    headers={"Content-Type": "application/json"},
                                                    body=body, completion=completion)
                del body
            else:
                submission = await asyncio.shield(ticket.submitted)
                if submission.response is None:
                    raise JobError("submission", "image handoff failed before submission")
                completion = ticket.completion
            job.status = "generating"
            stage = "generation"
            outcome = await asyncio.shield(completion)
            if outcome.error:
                if self.closing:
                    raise JobError("shutdown", "broker is shutting down")
                raise JobError("generation", "image generation did not complete successfully")
            job.status = "saving"
            stage = "result"
            job.images = await self._save(workflow, outcome.outputs)
            job.status = "succeeded"
        except asyncio.CancelledError:
            job.status = "failed"
            job.error = {"category": "shutdown", "message": "broker is shutting down"}
        except Exception as exc:
            job.status = "failed"
            if self.closing:
                category, message = "shutdown", "broker is shutting down"
            elif isinstance(exc, QueueFull):
                category, message = "queue_full", "workflow queue is full"
            elif isinstance(exc, JobError):
                category, message = exc.category, str(exc)
            else:
                category, message = stage, f"image job failed during {stage}"
            job.error = {"category": category, "message": message}
            LOG.error("MCP image job failed: stage=%s category=%s", stage, category)
        finally:
            values.clear()

    async def _save(self, workflow: Workflow, outputs: Any) -> list[dict[str, str]]:
        if not isinstance(outputs, dict):
            raise JobError("result", "image outputs are missing")
        sources = []
        # Resolve all short-lived Helto references before starting file transfers.
        for spec in workflow.outputs:
            node, adapter = spec["node"], spec["adapter"]
            entry = outputs.get(node)
            key = "images" if adapter == "comfy_images" else "helto_saved_images"
            records = entry.get(key) if isinstance(entry, dict) else None
            if not isinstance(records, list) or not records:
                raise JobError("result", "configured output has no images")
            if len(sources) + len(records) > MAX_IMAGES:
                raise JobError("result", "too many output images")
            for record in records:
                if not isinstance(record, dict):
                    raise JobError("result", "invalid image reference")
                if adapter == "helto_saved_images":
                    token = record.get("reference_token")
                    if not isinstance(token, str) or not 1 <= len(token) <= 128:
                        raise JobError("result", "invalid Helto image reference")
                    result = await self._json("/helto_save_image_advanced/resolve_reference",
                                              {"reference_token": token})
                    if (not isinstance(result, dict) or result.get("schema_version") != 1
                            or not isinstance(result.get("path"), str)
                            or not Path(result["path"]).is_absolute()
                            or result.get("media_type") != "image/png"):
                        raise JobError("result", "invalid resolved Helto reference")
                    sources.append((adapter, result, workflow.source_roots[node]))
                else:
                    filename, subfolder = record.get("filename"), record.get("subfolder", "")
                    if (not isinstance(filename, str) or not filename
                            or any(c in filename for c in ("/", "\\", "\x00"))
                            or filename in {".", ".."} or not isinstance(subfolder, str)
                            or PurePosixPath(subfolder).is_absolute()
                            or ".." in PurePosixPath(subfolder).parts
                            or "\\" in subfolder or "\x00" in subfolder
                            or record.get("type", "output") not in {"output", "temp"}):
                        raise JobError("result", "invalid ComfyUI image reference")
                    sources.append((adapter, {"filename": filename, "subfolder": subfolder,
                                              "type": record.get("type", "output")}, None))
        staging = self.output_dir / f".mcp-{uuid4().hex}.part"
        final = self.output_dir / uuid4().hex
        images = []
        try:
            await _disk(staging.mkdir, 0o700)
            for index, (adapter, source, root) in enumerate(sources):
                if adapter == "helto_saved_images":
                    mime, extension = "image/png", ".png"
                    path = staging / f"{index}{extension}"
                    await _disk(_copy_local, Path(source["path"]), path, root)
                else:
                    path, mime = await self._download(source, staging, index)
                images.append({"path": str(final / path.name), "mime_type": mime})
            await _disk(os.replace, staging, final)
        except BaseException:
            await _disk(shutil.rmtree, final, True)
            raise
        finally:
            await _disk(shutil.rmtree, staging, True)
        return images

    async def _download(self, params: dict[str, str], directory: Path,
                        index: int) -> tuple[Path, str]:
        async with asyncio.timeout(self.config.request_timeout):
            async with self.session.get(child_url(self.config.comfy_url, "/view"),
                                        params=params, allow_redirects=False,
                                        auto_decompress=True) as response:
                mime = response.content_type
                if response.status != 200 or mime not in MIME_EXTENSIONS:
                    raise JobError("result", "image download was rejected")
                path = directory / f"{index}{MIME_EXTENSIONS[mime]}"
                total = 0
                # Disk writes run outside the event loop and buffering stays bounded.
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    first = total == 0
                    total += len(chunk)
                    if total > MAX_IMAGE_BYTES:
                        raise JobError("result", "image exceeds the transfer limit")
                    await _disk(_write_chunk, path, chunk, first)
                if not total:
                    raise JobError("result", "image is empty")
                return path, mime

    def stop_accepting(self) -> None:
        self.closing = True

    async def close(self) -> None:
        self.stop_accepting()
        tasks = [job.task for job in self.jobs.values() if job.task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        for job in self.jobs.values():
            if job.task is not None and not job.task.done():
                job.status = "failed"
                job.error = {"category": "shutdown", "message": "broker is shutting down"}
        await asyncio.gather(*tasks, return_exceptions=True)
