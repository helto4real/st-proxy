from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from aiohttp import ClientError, ClientSession

from .config import BrokerConfig
from .errors import UpstreamError
from .http import BufferedResponse, child_url
from .llm.base import backend_error

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class PromptResult:
    response: BufferedResponse
    prompt_id: str | None


class ComfyClient:
    def __init__(self, session: ClientSession, config: BrokerConfig) -> None:
        self._session = session
        self._config = config

    async def submit(
        self,
        *,
        path: str,
        query_string: str,
        headers: dict[str, str],
        body: bytes,
    ) -> PromptResult:
        url = child_url(self._config.comfy_url, path)
        if query_string:
            url = f"{url}?{query_string}"
        try:
            async with asyncio.timeout(self._config.request_timeout):
                async with self._session.post(url, headers=headers, data=body) as response:
                    raw = await response.read()
                    buffered = BufferedResponse(
                        response.status,
                        response.reason,
                        tuple(response.headers.items()),
                        raw,
                    )
        except (ClientError, TimeoutError) as exc:
            raise backend_error("ComfyUI", "prompt submission", type(exc).__name__) from exc
        prompt_id = None
        if buffered.status < 400:
            try:
                payload = json.loads(raw)
                value = payload.get("prompt_id") if isinstance(payload, dict) else None
                prompt_id = str(value) if value is not None else None
            except (ValueError, TypeError):
                pass
        return PromptResult(buffered, prompt_id)

    async def wait_for_prompt(self, prompt_id: str) -> None:
        started = time.monotonic()
        deadline = time.monotonic() + self._config.image_timeout
        consecutive_failures = 0
        while time.monotonic() < deadline:
            failure: str | None = None
            try:
                per_request_timeout = min(
                    self._config.request_timeout, max(0.01, deadline - time.monotonic())
                )
                async with asyncio.timeout(per_request_timeout):
                    async with self._session.get(
                        child_url(self._config.comfy_url, f"/history/{prompt_id}")
                    ) as response:
                        if response.status >= 400:
                            failure = f"HTTP {response.status}"
                        else:
                            payload = await response.json(content_type=None)
                            if not isinstance(payload, dict):
                                failure = "invalid history response"
                                entry = None
                            else:
                                consecutive_failures = 0
                                entry = payload.get(prompt_id)
                            if isinstance(entry, dict):
                                status = entry.get("status")
                                if isinstance(status, dict):
                                    status_name = str(status.get("status_str", "")).lower()
                                    if status_name in {
                                        "cancelled",
                                        "canceled",
                                        "error",
                                        "failed",
                                        "failure",
                                    }:
                                        raise backend_error("ComfyUI", "image job", status_name)
                                    if status.get("completed") is True:
                                        if status_name not in {"success", "completed"}:
                                            raise backend_error(
                                                "ComfyUI",
                                                "image job",
                                                status_name or "unknown",
                                            )
                                        LOG.info(
                                            "ComfyUI job state confirmed: prompt_id=%s "
                                            "duration=%.3fs",
                                            prompt_id,
                                            time.monotonic() - started,
                                        )
                                        return
                                if entry.get("outputs") and not status:
                                    LOG.info(
                                        "ComfyUI job outputs confirmed: prompt_id=%s "
                                        "duration=%.3fs",
                                        prompt_id,
                                        time.monotonic() - started,
                                    )
                                    return
            except UpstreamError:
                raise
            except (ClientError, TimeoutError, ValueError) as exc:
                failure = type(exc).__name__
            if failure is not None:
                consecutive_failures += 1
                if consecutive_failures == 1:
                    LOG.warning(
                        "ComfyUI history monitoring failed; retrying: error=%s",
                        failure,
                    )
                if consecutive_failures >= self._config.comfy_poll_failure_limit:
                    LOG.error(
                        "ComfyUI history monitoring aborted after consecutive failures: "
                        "count=%s error=%s",
                        consecutive_failures,
                        failure,
                    )
                    await self.interrupt()
                    raise backend_error(
                        "ComfyUI",
                        "history monitoring",
                        f"{failure} after {consecutive_failures} consecutive failures",
                    )
            await asyncio.sleep(self._config.poll_interval)
        await self.interrupt()
        raise backend_error("ComfyUI", "image job", "timed out")

    async def interrupt(self) -> None:
        LOG.info("ComfyUI interrupt requested")
        try:
            async with asyncio.timeout(self._config.cleanup_timeout):
                async with self._session.post(
                    child_url(self._config.comfy_url, "/interrupt")
                ) as response:
                    await response.read()
                    LOG.info("ComfyUI interrupt completed: status=%s", response.status)
        except (ClientError, TimeoutError) as exc:
            LOG.warning("ComfyUI interrupt failed: %s", type(exc).__name__)

    async def free_models(self) -> None:
        LOG.info("ComfyUI VRAM cleanup requested")
        try:
            async with asyncio.timeout(self._config.cleanup_timeout):
                async with self._session.post(
                    child_url(self._config.comfy_url, "/free"),
                    json={"unload_models": True, "free_memory": True},
                ) as response:
                    await response.read()
                    if response.status >= 400:
                        raise backend_error(
                            "ComfyUI", "model cleanup", f"HTTP {response.status}"
                        )
                    LOG.info("ComfyUI VRAM cleanup completed: status=%s", response.status)
        except UpstreamError:
            raise
        except (ClientError, TimeoutError) as exc:
            raise backend_error("ComfyUI", "model cleanup", type(exc).__name__) from exc
