"""Optional MCP transport. GPU and job ownership belong to BrokerService."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import socket
from typing import Any

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from uvicorn import Config, Server

from .media_jobs import JobError, MediaJobs
from .workflows import WorkflowError

LOG = logging.getLogger(__name__)


class BearerAuth:
    def __init__(self, app, token: str) -> None:
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            provided = headers.get(b"authorization", b"")
            if not hmac.compare_digest(provided, b"Bearer " + self.token):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
        await self.app(scope, receive, send)


class BrokerMcpServer(Server):
    """The broker owns signals; expose startup completion without another loop."""
    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.ready = asyncio.Event()

    @contextlib.contextmanager
    def capture_signals(self):
        yield

    async def startup(self, sockets=None) -> None:
        await super().startup(sockets=sockets)
        if self.started:
            self.ready.set()


class McpListener:
    def __init__(self, jobs: MediaJobs, port: int, token: str | None = None) -> None:
        self.jobs, self.port, self.token = jobs, port, token
        self.server: BrokerMcpServer | None = None
        self.task: asyncio.Task[None] | None = None
        self.socket: socket.socket | None = None

    def _app(self):
        sdk = MCPServer("st-proxy", instructions=(
            "Generate or upscale images using approved workflows. First call "
            "describe_workflow for the chosen workflow. If its schema exposes prompt, apply "
            "any prompt_guidance to parameters.prompt. Otherwise use the stored workflow prompt. "
            "For image_upscale, register exactly one image and pass optional max_side and seed. "
            "For workflows with random seed defaults, omit seed for a fresh seed per image "
            "or provide an integer for a fixed seed. Results contain the resolved seed. "
            "The nsfw flag is an opt-in LoRA switch, not a content label. Set parameters.nsfw=true "
            "only if exposed and the user explicitly requests enabling the NSFW mode, flag or LoRA "
            "for this image or batch, e.g. 'enable NSFW mode' or 'nsfw=true'. "
            "Requests for NSFW, nude, erotic or explicit content, including 'make an NSFW image', "
            "do not authorize this switch. Never infer activation from prompts, prompt guidance, "
            "references or earlier jobs. Without a clear activation request, omit nsfw or set "
            "false and proceed with the requested image. It is independent of draft/standard mode. "
            "When multiple images are needed, prefer "
            "generate_batch: it is the most efficient way to avoid repeated GPU switching and "
            "slow LLM reloads. Prepare all items before calling it. "
            "Choose a unique job_id before generate or generate_batch. "
            "If a call times out, use get_job with that same ID; do not start another generation. "
            "Images are local files. Only an active LLM request triggers GPU restoration."
        ))
        readonly = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                   idempotent_hint=True, open_world_hint=False)
        generation = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                     idempotent_hint=True, open_world_hint=False)

        def result(payload: dict[str, Any]) -> CallToolResult:
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))],
                                  structured_content=payload,
                                  is_error=bool(payload.get("error")))

        def error(exc: JobError | WorkflowError, job_id: str | None = None) -> CallToolResult:
            return result({"job_id": job_id, "error": {
                "category": exc.category if isinstance(exc, JobError) else "validation",
                "message": str(exc),
            }})

        @sdk.tool(annotations=readonly)
        def list_workflows() -> CallToolResult:
            """List approved image workflows; describe one for parameters and prompt guidance."""
            workflows = [w.summary() for w in self.jobs.catalog.workflows.values()]
            return result({"workflows": workflows})

        @sdk.tool(annotations=readonly)
        def describe_workflow(workflow_id: str) -> CallToolResult:
            """Get parameters, available modes, reference limits and optional prompt_guidance.

            No GPU acquisition. Select parameters.mode from its enum when available.
            nsfw is an opt-in LoRA switch, not a content label. Set true only when the user
            explicitly requests enabling the NSFW mode, flag or LoRA for this image or batch.
            NSFW content requests or prompt guidance do not authorize it;
            otherwise omit nsfw or set false.
            Write parameters.prompt only if exposed, applying any prompt guidance.
            Workflows without a prompt parameter use their stored prompt.
            """
            try:
                return result(self.jobs.catalog.get(workflow_id).describe())
            except WorkflowError as exc:
                return error(exc)

        @sdk.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                              idempotent_hint=False, open_world_hint=False))
        async def register_image(workflow_id: str, path: str) -> CallToolResult:
            """Register a local PNG/JPEG/WebP reference without acquiring the GPU.

            Use an absolute path under the configured MCP input or output directory.
            For variants, register the selected result's path with qwen_scene, even if
            it was generated by image_generation without references. A saved result path
            is not an image_id; reuse the returned ID across all variant batch items.
            Returns an image_id and preview_path. IDs last until the proxy restarts and
            belong to this workflow. Pass IDs in order: <image1>, <image2>, etc.
            For upscaling, register with image_upscale and supply exactly one returned ID.
            Never send image bytes or base64 in prompts. Limit: 10 MiB per image.
            """
            try:
                return result(await self.jobs.register_image(workflow_id, path))
            except (JobError, WorkflowError) as exc:
                return error(exc)

        @sdk.tool(annotations=generation)
        async def generate(job_id: str, workflow_id: str,
                           parameters: dict[str, Any],
                           references: list[str] | None = None) -> CallToolResult:
            """Generate or upscale and wait for saved images.

            Prefer generate_batch for multiple images.

            References are registered image IDs; required counts come from describe_workflow.
            image_upscale requires one reference, no prompt, and optional max_side and seed.
            Choose parameters.mode from describe_workflow, or omit it for the configured default.
            If exposed, nsfw is an opt-in LoRA switch, default false, not a content label.
            Set true only when the user explicitly requests enabling the NSFW mode, flag or LoRA
            for this image (e.g. 'enable NSFW mode' or 'nsfw=true'). 'Make an NSFW image' or a
            request for nude, erotic or explicit content does not authorize it. Never infer
            activation from prompts, prompt guidance, references or earlier jobs.
            Without a clear activation request, omit nsfw or set false and proceed with the image.
            Omit seed for a random default where described, or supply a fixed integer.
            The resolved seed is returned in parameters and retained on job-ID retries.
            Reuse job_id to recover a lost response.
            """
            try:
                return result(await self.jobs.generate(job_id, workflow_id, parameters, references))
            except (JobError, WorkflowError) as exc:
                return error(exc, job_id)

        @sdk.tool(annotations=generation)
        async def generate_batch(job_id: str, workflow_id: str,
                                 items: list[dict[str, Any]]) -> CallToolResult:
            """Generate or upscale multiple images without LLM reloads between items.

            Prefer this tool whenever multiple images are needed. Prepare all items first.
            Each item is a parameter object for the same workflow, for example
            [{"prompt": "a forest"}, {"prompt": "a lake", "aspect_ratio": "16:9"}].
            With references, use {"parameters": {"prompt": "..."}, "references": ["image_id"]}
            per item. For image_upscale use {"parameters": {"max_side": 3200},
            "references": ["image_id"]}; parameters may be empty for default size/random seed.
            Reference order maps to <image1>, <image2>, etc. for that item.
            For variants of one saved image, use qwen_scene and the same registered ID
            as the sole reference in every item (<image1>). Write each complete prompt
            using prompt_guidance, specifying what to change and preserve. All variants
            use that original image, not another item in this batch.
            Each item may select its own mode from describe_workflow via parameters.mode
            (or mode directly in a flat item); omitting it uses the configured default.
            Each item has its own nsfw flag when exposed: an opt-in LoRA switch, default false,
            not a content label. Set true only when the user explicitly requests enabling the
            NSFW mode, flag or LoRA for that image or this batch (e.g. 'enable NSFW mode' or
            'nsfw=true'). 'Make NSFW images' or requests for nude, erotic or explicit content
            do not authorize it. Never infer activation from prompts, prompt guidance, references,
            earlier jobs or another item's flag. Without a clear activation request for an item,
            omit nsfw or set false and proceed with it. It is independent of parameters.mode.
            Items run consecutively in FIFO order, within the configured image queue limit
            (32 by default). All parameters are validated before anything is queued.
            Results have zero-based item indices, individual status and saved image paths;
            successful items are retained if others fail. Defaults, including seed, are
            controlled by the workflow schema. Random-default seeds are chosen separately
            per item; explicit seeds remain fixed. Results include each resolved seed.
            Wait for the whole batch. After timeout use get_job with the same job_id;
            never resubmit successful items under a new ID.
            """
            try:
                return result(await self.jobs.generate_batch(job_id, workflow_id, items))
            except (JobError, WorkflowError) as exc:
                return error(exc, job_id)

        @sdk.tool(annotations=readonly)
        async def get_job(job_id: str, wait: bool = True) -> CallToolResult:
            """Recover a job in this process. Wait by default; false returns current status."""
            try:
                return result(await self.jobs.get_job(job_id, wait))
            except JobError as exc:
                return error(exc, job_id)

        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"127.0.0.1:{self.port}", f"localhost:{self.port}"],
            allowed_origins=[f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"],
        )
        app = sdk.streamable_http_app(stateless_http=True, transport_security=security)
        return BearerAuth(app, self.token) if self.token else app

    async def start(self) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.socket.bind(("127.0.0.1", self.port))
            self.socket.setblocking(False)
            self.port = self.socket.getsockname()[1]
            config = Config(self._app(), host="127.0.0.1", port=self.port, log_config=None,
                            access_log=False, lifespan="on", timeout_graceful_shutdown=2,
                            ws="none")
            self.server = BrokerMcpServer(config)
            self.task = asyncio.create_task(self.server.serve(sockets=[self.socket]),
                                            name="st-proxy-mcp")
            ready = asyncio.create_task(self.server.ready.wait())
            try:
                async with asyncio.timeout(10):
                    await asyncio.wait((ready, self.task), return_when=asyncio.FIRST_COMPLETED)
                    if self.task.done():
                        await self.task
                        raise RuntimeError("MCP server stopped during startup")
            finally:
                ready.cancel()
                await asyncio.gather(ready, return_exceptions=True)
        except BaseException:
            await self.close()
            raise
        LOG.info("MCP ready: http://127.0.0.1:%s/mcp", self.port)

    async def close(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.task is not None:
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.socket is not None:
            self.socket.close()
            self.socket = None
