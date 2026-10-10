from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector, web

from .comfy import ComfyClient
from .comfy_routes import ComfyRouteKind, classify_comfy_route, comfy_upstream_path
from .config import BrokerConfig
from .coordinator import HandoffCoordinator
from .errors import BrokerError, ChatUnavailable, ConfigurationError, HandoffError, QueueFull
from .http import BufferedResponse, proxy_stream, proxy_websocket, upstream_request_headers
from .kobold_router import ABORT_PATH, TEXT_PATHS, default_model_body, read_model_cache
from .llm import LlmBackend, build_llm_backend
from .llm.koboldcpp import KoboldCppBackend
from .llm.multi import MultiBackend, MultiTarget
from .llm.prism import PrismBackend, PrismRestorePoint
from .llm.prism_diagnostics import PrismDiagnostics, request_usage
from .llm.strata import STRATA_TEXT_PATHS, StrataBackend, StrataRestorePoint
from .llm.tabbyapi import adapt_chat_request
from .media_jobs import MediaJobs
from .model_profiles import ModelProfiles
from .workflows import WorkflowCatalog

LOG = logging.getLogger(__name__)
STATUS_PATH = "/broker/status"
CONTROL_CONNECTION_LIMIT = 16
CHAT_CONNECTION_LIMIT = 64
IMAGE_CONNECTION_LIMIT = 64
WEBSOCKET_CONNECTION_LIMIT = 32


class WorkflowBodyTooLarge(ValueError):
    """Raised before a workflow body can grow beyond its configured limit."""


def _tabby_thinking_diagnostics(body: bytes) -> tuple[str, ...]:
    """Allowlist only the three outgoing fields, never arbitrary request text."""
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        return ("<other>",) * 3
    if not isinstance(payload, dict):
        return ("<other>",) * 3

    def safe_value(key: str) -> str:
        if key not in payload:
            return "<absent>"
        value = payload[key]
        if value is None:
            return "null"
        if key == "reasoning_effort":
            if isinstance(value, str) and value in {
                "minimal", "low", "medium", "high", "none", "off", "unset",
            }:
                return value
        elif type(value) is int or (type(value) is float and math.isfinite(value)):
            return str(value)
        return "<other>"

    return tuple(safe_value(key) for key in (
        "reasoning_effort", "max_tokens", "reasoning_budget_tokens",
    ))


def _is_routine_comfy_poll(request: web.Request) -> bool:
    return request.method == "GET" and (
        request.path == "/history"
        or request.path.startswith("/history/")
        or request.path == "/api/jobs"
        or request.path.endswith("/jobs/status")
    )


def _is_tabby_service_metadata(request: web.Request, backend_kind: str) -> bool:
    # Service health and model discovery do not require a loaded model or GPU ownership.
    return (
        backend_kind == "tabbyapi"
        and request.method in {"GET", "HEAD"}
        and request.path.rstrip("/") in {"/v1/models", "/v1/model/list", "/health"}
    )


def _is_passive_llm_metadata(request: web.Request, backend_kind: str) -> bool:
    if backend_kind == "tabbyapi":
        return request.method in {"GET", "HEAD"} and request.path.rstrip("/") in {
            "/v1/model", "/props",
        }
    return (
        backend_kind == "koboldcpp"
        and request.method == "GET"
        and request.path in {"/api/v1/model", "/v1/models"}
    )


def _inactive_llm_metadata(path: str, backend_kind: str = "koboldcpp") -> web.Response:
    if backend_kind == "tabbyapi":
        return web.json_response({"detail": "LLM readiness is not currently verified"}, status=503)
    if path == "/v1/models":
        return web.json_response({"object": "list", "data": []})
    return web.json_response({"result": "inactive"})


def _history_delete_prompt_ids(body: bytes) -> frozenset[str]:
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return frozenset()
    if not isinstance(payload, dict) or not isinstance(payload.get("delete"), list):
        return frozenset()
    return frozenset(
        value for value in payload["delete"] if isinstance(value, str) and value
    )


def _bound_port(site: web.TCPSite) -> int:
    server = site._server  # aiohttp has no public bound-port accessor
    if server is None or not server.sockets:
        raise RuntimeError("site did not bind a socket")
    return int(server.sockets[0].getsockname()[1])


async def _read_workflow_body(request: web.Request, max_bytes: int) -> bytes:
    if request.content_length is not None and request.content_length > max_bytes:
        raise WorkflowBodyTooLarge
    body = bytearray()
    async for chunk in request.content.iter_chunked(64 * 1024):
        if len(body) + len(chunk) > max_bytes:
            raise WorkflowBodyTooLarge
        body.extend(chunk)
    return bytes(body)


class BrokerService:
    def __init__(self, config: BrokerConfig) -> None:
        self.config = config
        self.model_profiles = ModelProfiles()
        self.control_session: ClientSession | None = None
        self.chat_session: ClientSession | None = None
        self.image_session: ClientSession | None = None
        self.websocket_session: ClientSession | None = None
        self.llm: LlmBackend[Any] | None = None
        self.coordinator: HandoffCoordinator | None = None
        self.media_jobs: MediaJobs | None = None
        self.mcp_listener = None
        self._chat_runner: web.AppRunner | None = None
        self._image_runner: web.AppRunner | None = None
        self._chat_site: web.TCPSite | None = None
        self._image_site: web.TCPSite | None = None
        self.chat_port: int | None = None
        self.image_port: int | None = None
        self._active_chat_requests = 0
        self._active_image_requests = 0
        self._active_websockets = 0
        self._router_models = (
            read_model_cache(config.kobold_model_cache) if config.kobold_router_mode else None
        )

    def _new_session(
        self,
        connection_limit: int,
        *,
        force_close: bool = False,
    ) -> ClientSession:
        timeout = ClientTimeout(
            total=None,
            connect=self.config.connect_timeout,
            sock_connect=self.config.connect_timeout,
            sock_read=self.config.request_timeout,
        )
        connector = (
            TCPConnector(
                limit=connection_limit,
                limit_per_host=connection_limit,
                force_close=True,
            )
            if force_close
            else TCPConnector(
                limit=connection_limit,
                limit_per_host=connection_limit,
                keepalive_timeout=min(15.0, self.config.request_timeout / 2),
            )
        )
        return ClientSession(
            timeout=timeout,
            connector=connector,
            auto_decompress=False,
        )

    @web.middleware
    async def _chat_activity_middleware(self, request: web.Request, handler):
        if request.path == STATUS_PATH:
            return await handler(request)
        self._active_chat_requests += 1
        try:
            return await handler(request)
        finally:
            self._active_chat_requests -= 1

    @web.middleware
    async def _image_activity_middleware(self, request: web.Request, handler):
        if request.path == STATUS_PATH:
            return await handler(request)
        is_websocket = request.headers.get("Upgrade", "").lower() == "websocket"
        if is_websocket:
            self._active_websockets += 1
        else:
            self._active_image_requests += 1
        try:
            return await handler(request)
        finally:
            if is_websocket:
                self._active_websockets -= 1
            else:
                self._active_image_requests -= 1

    async def start(self) -> None:
        try:
            self.model_profiles = await asyncio.to_thread(ModelProfiles, self.config.model_profiles)
            catalog = None
            if self.config.mcp_port is not None:
                try:
                    from .mcp_server import McpListener
                except ImportError:
                    raise ConfigurationError("MCP requires installation with .[mcp]") from None
                catalog = await asyncio.to_thread(
                    WorkflowCatalog, self.config.mcp_workflows_dir,
                    self.config.max_workflow_body_bytes,
                )
            self.control_session = self._new_session(
                CONTROL_CONNECTION_LIMIT,
                force_close=True,
            )
            self.chat_session = self._new_session(CHAT_CONNECTION_LIMIT)
            self.image_session = self._new_session(IMAGE_CONNECTION_LIMIT)
            self.websocket_session = self._new_session(WEBSOCKET_CONNECTION_LIMIT)
            self.llm = build_llm_backend(self.control_session, self.config)
            self.coordinator = HandoffCoordinator(
                self.config,
                self.llm,
                ComfyClient(self.control_session, self.config),
            )
            await self.coordinator.initialize()
            chat_app = web.Application(
                client_max_size=1024**3,
                middlewares=[self._chat_activity_middleware],
            )
            image_app = web.Application(
                client_max_size=1024**3,
                middlewares=[self._image_activity_middleware],
            )
            chat_app.router.add_route("*", "/{tail:.*}", self._chat_handler)
            image_app.router.add_route("*", "/{tail:.*}", self._image_handler)
            self._chat_runner = web.AppRunner(chat_app, access_log=None)
            self._image_runner = web.AppRunner(image_app, access_log=None)
            await self._chat_runner.setup()
            await self._image_runner.setup()
            self._chat_site = web.TCPSite(
                self._chat_runner, self.config.listen_host, self.config.chat_port
            )
            self._image_site = web.TCPSite(
                self._image_runner, self.config.listen_host, self.config.image_port
            )
            await self._chat_site.start()
            await self._image_site.start()
            if catalog is not None:
                self.media_jobs = MediaJobs(self.config, catalog, self.coordinator,
                                            self.image_session)
                await self.media_jobs.start()
                self.mcp_listener = McpListener(
                    self.media_jobs, self.config.mcp_port, self.config.mcp_token,
                )
                await self.mcp_listener.start()
        except BaseException:
            await self.stop()
            raise
        self.chat_port = _bound_port(self._chat_site)
        self.image_port = _bound_port(self._image_site)

    async def _status(self) -> web.Response:
        assert self.coordinator is not None
        status = self.coordinator.status()
        status.update(
            {
                "active_chat_requests": self._active_chat_requests,
                "active_image_requests": self._active_image_requests,
                "active_websockets": self._active_websockets,
                "control_connection_limit": CONTROL_CONNECTION_LIMIT,
                "chat_connection_limit": CHAT_CONNECTION_LIMIT,
                "image_connection_limit": IMAGE_CONNECTION_LIMIT,
                "websocket_connection_limit": WEBSOCKET_CONNECTION_LIMIT,
            }
        )
        if isinstance(self.llm, MultiBackend):
            active = self.llm.active
            status["active_llm_backend"] = active.engine if active else None
            status["active_model"] = f"{active.engine}/{active.model}" if active else None
        return web.json_response(status)

    async def _submit_image_until_disconnect(
        self,
        request: web.Request,
        *,
        path: str,
        query_string: str,
        headers: dict[str, str],
        body: bytes,
    ) -> BufferedResponse:
        assert self.coordinator is not None
        submission = asyncio.create_task(
            self.coordinator.submit_image(
                path=path,
                query_string=query_string,
                headers=headers,
                body=body,
            ),
            name="st-proxy-workflow-submission",
        )
        try:
            while True:
                done, _pending = await asyncio.wait((submission,), timeout=0.05)
                if done:
                    return submission.result()
                transport = request.transport
                if transport is None or transport.is_closing():
                    submission.cancel()
                    await asyncio.gather(submission, return_exceptions=True)
                    raise asyncio.CancelledError
        finally:
            if not submission.done():
                submission.cancel()
                await asyncio.gather(submission, return_exceptions=True)

    async def _chat_handler(self, request: web.Request) -> web.StreamResponse:
        assert self.coordinator is not None and self.chat_session is not None
        if request.path == STATUS_PATH:
            return await self._status()
        started = time.monotonic()
        assert self.llm is not None
        label = self.llm.info.label
        LOG.info(
            "request started: target=%s method=%s path=%s",
            label,
            request.method,
            request.path,
        )
        try:
            if isinstance(self.llm, MultiBackend):
                response = await self._multi_handler(request)
            elif self.config.kobold_router_mode:
                response = await self._router_handler(request)
            elif isinstance(self.llm, (PrismBackend, StrataBackend)):
                response = await self._managed_model_handler(request)
            elif _is_tabby_service_metadata(request, self.llm.info.kind):
                response = await proxy_stream(
                    request,
                    self.chat_session,
                    self.llm.info.chat_origin,
                    target=label,
                )
            elif _is_passive_llm_metadata(request, self.llm.info.kind):
                LOG.debug(
                    "passive LLM metadata request bypasses GPU lease: path=%s",
                    request.path,
                )
                async with self.coordinator.passive_llm_metadata() as available:
                    if available:
                        response = await proxy_stream(
                            request,
                            self.chat_session,
                            self.llm.info.chat_origin,
                            target=label,
                        )
                    else:
                        response = _inactive_llm_metadata(request.path, self.llm.info.kind)
            else:
                async with self.coordinator.chat_lease():
                    body = None
                    if (
                        self.llm.info.kind == "tabbyapi"
                        and request.method == "POST"
                        and request.path.rstrip("/") == "/v1/chat/completions"
                    ):
                        body = self.model_profiles.apply_body(
                            await self._chat_body(request), self.llm.info.kind,
                        )
                        body = adapt_chat_request(body)
                        if len(body) > self.config.max_chat_body_bytes:
                            raise web.HTTPRequestEntityTooLarge(
                                max_size=self.config.max_chat_body_bytes, actual_size=len(body))
                        LOG.info(
                            "TabbyAPI request: reasoning_effort=%s max_tokens=%s "
                            "reasoning_budget_tokens=%s",
                            *_tabby_thinking_diagnostics(body),
                        )
                    response = await proxy_stream(
                        request,
                        self.chat_session,
                        self.llm.info.chat_origin,
                        target=label,
                        body=body,
                    )
            LOG.info(
                "request completed: target=%s method=%s path=%s status=%s duration=%.3fs",
                label,
                request.method,
                request.path,
                response.status,
                time.monotonic() - started,
            )
            return response
        except ChatUnavailable as exc:
            LOG.error(
                "request rejected: target=%s method=%s path=%s status=503 reason=%s "
                "duration=%.3fs",
                label,
                request.method,
                request.path,
                exc,
                time.monotonic() - started,
            )
            return web.json_response({"error": str(exc)}, status=503)
        except (BrokerError, ClientError, TimeoutError) as exc:
            LOG.error(
                "request failed: target=%s method=%s path=%s status=502 "
                "error=%s duration=%.3fs",
                label,
                request.method,
                request.path,
                type(exc).__name__,
                time.monotonic() - started,
            )
            return web.json_response(
                {"error": f"{label} upstream request failed"},
                status=502,
            )

    async def _multi_handler(self, request: web.Request) -> web.StreamResponse:
        assert isinstance(self.llm, MultiBackend)
        assert self.coordinator is not None and self.chat_session is not None
        path = request.path.rstrip("/")
        if ("autoload" in request.query or "reload" in request.query
                or path.startswith(("/api/admin/", "/models/", "/v1/model/"))
                or path == "/noscript"):
            raise web.HTTPForbidden(text="model lifecycle is owned by the broker")
        if request.method in {"GET", "HEAD"} and path in {"/v1/models", "/models"}:
            return web.json_response(await self.llm.model_catalog())
        if request.method in {"GET", "HEAD"} and path == "/health":
            return await self._status()
        body = await self._chat_body(request) if request.can_read_body else b""
        try:
            payload = json.loads(body) if body else None
            if payload is not None and not isinstance(payload, dict):
                raise ValueError
        except (ValueError, UnicodeError, RecursionError):
            raise web.HTTPBadRequest(text="expected a JSON object") from None
        model = payload.get("model") if payload is not None else request.query.get("model")
        query_model = request.query.get("model")
        if query_model is not None and payload is not None and query_model != model:
            raise web.HTTPBadRequest(text="conflicting body and query model IDs")
        abort = request.method == "POST" and path == ABORT_PATH
        if model is None and (abort or request.method in {"GET", "HEAD", "OPTIONS"}):
            target = self.llm.active
            if target is None:
                raise ChatUnavailable("no active model")
        else:
            try:
                target = await self.llm.request_target(model)
            except ValueError as exc:
                raise web.HTTPBadRequest(text=str(exc)) from None
        backend = self.llm.backends[target.engine]
        if payload is not None:
            payload["model"] = target.model
            body = json.dumps(payload, ensure_ascii=False).encode()
        query = request.rel_url.update_query(model=target.model).raw_query_string
        if request.method in {"GET", "HEAD", "OPTIONS"} or abort:
            if abort and target.engine != "koboldcpp":
                raise web.HTTPNotFound()
            async with self.coordinator.router_control(abort=abort) as available:
                if not available or self.llm.active != target:
                    raise ChatUnavailable("requested model is not active")
                return await proxy_stream(
                    request, self.chat_session, backend.info.chat_origin,
                    body=body, raw_query=query, target=backend.info.label,
                )
        # Model-dependent API calls are admitted; lifecycle routes cannot acquire
        # arbitrary extra models outside the shared GPU reservation.
        active_paths = set(TEXT_PATHS)
        if isinstance(backend, StrataBackend):
            active_paths = STRATA_TEXT_PATHS
        if target.engine == "prism":
            active_paths.update({"/completion", "/tokenize", "/detokenize", "/apply-template",
                                 "/v1/embeddings", "/embeddings", "/rerank", "/v1/rerank"})
        if request.method != "POST" or path not in active_paths:
            raise web.HTTPForbidden(text="unsupported model request in all-backend mode")
        if payload is None:
            raise web.HTTPBadRequest(text="expected a JSON object with model")
        if path == "/v1/chat/completions":
            profiled = self.model_profiles.apply(payload, target.engine, target.model)
            if profiled is not payload:
                payload = profiled
                body = json.dumps(payload, ensure_ascii=False).encode()
        if target.engine == "koboldcpp":
            body = default_model_body(body, self.config.kobold_default_model)
        if target.engine == "tabbyapi" and path == "/v1/chat/completions":
            body = adapt_chat_request(body)
        if target.engine == "prism" and path == "/v1/chat/completions":
            body = request_usage(payload)
        if len(body) > self.config.max_chat_body_bytes:
            raise web.HTTPRequestEntityTooLarge(
                max_size=self.config.max_chat_body_bytes, actual_size=len(body))
        acquired = asyncio.Event()
        task = asyncio.create_task(self._multi_chat(request, target, body, acquired))
        try:
            while not task.done() and not acquired.is_set():
                if request.transport is None or request.transport.is_closing():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    return web.Response(status=499)
                await asyncio.wait({task}, timeout=0.05)
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _multi_chat(
        self, request: web.Request, target: MultiTarget, body: bytes, acquired: asyncio.Event,
    ) -> web.StreamResponse:
        assert isinstance(self.llm, MultiBackend)
        assert self.coordinator is not None and self.chat_session is not None
        backend = self.llm.backends[target.engine]
        async with self.coordinator.chat_lease(target):
            acquired.set()
            if request.transport is None or request.transport.is_closing():
                return web.Response(status=499)
            path = request.path.rstrip("/")
            if target.engine == "koboldcpp":
                path = TEXT_PATHS.get(path, path)
            try:
                response = await proxy_stream(
                    request, self.chat_session, backend.info.chat_origin, body=body,
                    raw_path=path,
                    raw_query=request.rel_url.update_query(model=target.model).raw_query_string,
                    target=backend.info.label,
                    observer=(PrismDiagnostics(body) if target.engine == "prism"
                              and path == "/v1/chat/completions" else None),
                )
                if target.engine == "koboldcpp" and response.status >= 500:
                    self.llm.request_failed(target)
                    await self.coordinator.router_request_failed()
                return response
            except BaseException:
                if target.engine == "koboldcpp":
                    self.llm.request_failed(target)
                    await self.coordinator.router_request_failed()
                raise

    async def _managed_model_handler(self, request: web.Request) -> web.StreamResponse:
        assert self.coordinator is not None and self.chat_session is not None
        assert self.llm is not None
        path = request.path.rstrip("/")
        strata = isinstance(self.llm, StrataBackend)
        if strata and not (
            (request.method == "POST" and path in STRATA_TEXT_PATHS)
            or (request.method in {"GET", "HEAD"} and path in {
                "/health", "/api/health", "/models", "/v1/models", "/v1/status",
                "/props", "/slots",
            })
        ):
            raise web.HTTPForbidden(text="unsupported request or broker-owned lifecycle")
        # Router reload/autoload overrides and lifecycle operations must not bypass
        # the broker's GPU handoff, including model-list refresh via GET.
        if (
            "autoload" in request.query or "reload" in request.query
            or path.startswith("/models/")
            or (path in {"/models", "/v1/models", "/props"}
                and request.method not in {"GET", "HEAD"})
        ):
            raise web.HTTPForbidden(text="model lifecycle is owned by the broker")
        if request.method in {"GET", "HEAD"} and path in {
            "/health", "/api/health", "/models", "/v1/models", "/v1/status",
        }:
            return await proxy_stream(
                request, self.chat_session, self.llm.info.chat_origin,
                target=self.llm.info.label,
            )
        if request.method in {"GET", "HEAD"} and path in {"/props", "/metrics", "/slots"}:
            async with self.coordinator.passive_llm_metadata() as available:
                assert isinstance(self.llm, (PrismBackend, StrataBackend))
                active = await self.llm.observe_ready() if available else None
                requested = request.query.get("model")
                if active is None or (requested is not None and requested != active.model):
                    return web.json_response(
                        {"error": f"{self.llm.info.label} model is not active"}, status=503)
                return await proxy_stream(
                    request, self.chat_session, self.llm.info.chat_origin,
                    raw_query=request.rel_url.update_query(model=active.model).raw_query_string,
                    target=self.llm.info.label,
                )
        assert isinstance(self.llm, (PrismBackend, StrataBackend))
        body = await self._chat_body(request) if request.can_read_body else b""
        payload = None
        if body:
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError, RecursionError):
                raise web.HTTPBadRequest(text="expected a JSON object") from None
            if not isinstance(payload, dict):
                raise web.HTTPBadRequest(text="expected a JSON object")
        model = payload.get("model") if payload is not None else request.query.get("model")
        query_model = request.query.get("model")
        if payload is not None and query_model is not None and query_model != model:
            raise web.HTTPBadRequest(text="conflicting body and query model IDs")
        try:
            target = await self.llm.request_target(model)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from None
        if payload is not None and model is None:
            payload["model"] = target.model
            body = json.dumps(payload).encode()
        if request.method == "POST" and path == "/v1/chat/completions" and payload is not None:
            profiled = self.model_profiles.apply(payload, self.llm.info.kind, target.model)
            if profiled is not payload:
                payload = profiled
                body = json.dumps(payload, ensure_ascii=False).encode()
        if not strata and payload is not None and path == "/v1/chat/completions":
            body = request_usage(payload)
        if len(body) > self.config.max_chat_body_bytes:
            raise web.HTTPRequestEntityTooLarge(
                max_size=self.config.max_chat_body_bytes, actual_size=len(body))
        acquired = asyncio.Event()
        task = asyncio.create_task(self._managed_model_chat(request, target, body, acquired))
        try:
            while not task.done() and not acquired.is_set():
                if request.transport is None or request.transport.is_closing():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    return web.Response(status=499)
                await asyncio.wait({task}, timeout=0.05)
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _managed_model_chat(
        self, request: web.Request, target: PrismRestorePoint | StrataRestorePoint, body: bytes,
        acquired: asyncio.Event,
    ) -> web.StreamResponse:
        assert self.coordinator is not None and self.chat_session is not None
        assert self.llm is not None
        async with self.coordinator.chat_lease(target):
            acquired.set()
            if request.transport is None or request.transport.is_closing():
                return web.Response(status=499)
            return await proxy_stream(
                request, self.chat_session, self.llm.info.chat_origin, body=body,
                raw_query=request.rel_url.update_query(model=target.model).raw_query_string,
                target=self.llm.info.label,
                observer=(PrismDiagnostics(body)
                          if isinstance(self.llm, PrismBackend)
                          and request.path.rstrip("/") == "/v1/chat/completions"
                          and body else None),
            )

    async def _chat_body(self, request: web.Request) -> bytes:
        try:
            async with asyncio.timeout(self.config.request_timeout):
                return await _read_workflow_body(request, self.config.max_chat_body_bytes)
        except WorkflowBodyTooLarge:
            raise web.HTTPRequestEntityTooLarge(
                max_size=self.config.max_chat_body_bytes,
                actual_size=request.content_length or self.config.max_chat_body_bytes + 1,
            ) from None

    async def _router_handler(self, request: web.Request) -> web.StreamResponse:
        assert self.coordinator is not None and self.chat_session is not None
        assert isinstance(self.llm, KoboldCppBackend)
        path = request.path.rstrip("/")
        if request.method == "GET" and path in {"/v1/models", "/api/v1/model"}:
            async with self.coordinator.passive_llm_metadata() as available:
                if path == "/v1/models":
                    if available:
                        try:
                            self._router_models = await self.llm.router_models()
                        except BrokerError:
                            LOG.error("router model discovery failed; retaining cached list")
                    return web.json_response(self._router_models)
                if not available:
                    return _inactive_llm_metadata(path)
                return await proxy_stream(
                    request, self.chat_session, self.llm.info.chat_origin,
                    target=self.llm.info.label,
                )
        if path.startswith("/api/admin/reload_config") or path == "/noscript":
            raise web.HTTPForbidden(text="model lifecycle is owned by the broker")
        abort = request.method == "POST" and path == ABORT_PATH
        if request.method in {"GET", "HEAD", "OPTIONS"} or abort:
            async with self.coordinator.router_control(abort=abort) as available:
                if not available:
                    raise ChatUnavailable("router control is unavailable during GPU transition")
                body = await self._chat_body(request) if request.can_read_body else b""
                return await proxy_stream(
                    request, self.chat_session, self.llm.info.chat_origin, body=body,
                    target=self.llm.info.label,
                )
        if request.content_length is not None:
            if request.content_length > self.config.max_chat_body_bytes:
                raise web.HTTPRequestEntityTooLarge(
                    max_size=self.config.max_chat_body_bytes, actual_size=request.content_length,
                )
        acquired = asyncio.Event()
        task = asyncio.create_task(self._router_chat(request, acquired))
        try:
            while not task.done() and not acquired.is_set():
                if request.transport is None or request.transport.is_closing():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    return web.Response(status=499)
                await asyncio.wait({task}, timeout=0.05)
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _router_chat(
        self, request: web.Request, acquired: asyncio.Event,
    ) -> web.StreamResponse:
        assert self.coordinator is not None and self.chat_session is not None
        assert self.llm is not None
        async with self.coordinator.chat_lease():
            acquired.set()
            if request.transport is None or request.transport.is_closing():
                return web.Response(status=499)
            body = await self._chat_body(request)
            path = request.path.rstrip("/")
            if request.method == "POST" and path in TEXT_PATHS:
                try:
                    body = default_model_body(body, self.config.kobold_default_model)
                except (ValueError, UnicodeError):
                    raise web.HTTPBadRequest(text="expected a JSON object") from None
                path = TEXT_PATHS[path]
            if len(body) > self.config.max_chat_body_bytes:
                raise web.HTTPRequestEntityTooLarge(
                    max_size=self.config.max_chat_body_bytes, actual_size=len(body),
                )
            try:
                response = await proxy_stream(
                    request, self.chat_session, self.llm.info.chat_origin,
                    raw_path=path, body=body,
                    target=self.llm.info.label,
                )
                if response.status >= 500:
                    await self.coordinator.router_request_failed()
                return response
            except BaseException:
                await self.coordinator.router_request_failed()
                raise

    async def _image_handler(self, request: web.Request) -> web.StreamResponse:
        assert (
            self.coordinator is not None
            and self.control_session is not None
            and self.image_session is not None
            and self.websocket_session is not None
        )
        if request.path == STATUS_PATH:
            return await self._status()
        started = time.monotonic()
        routine_poll = _is_routine_comfy_poll(request)
        request_log = LOG.debug if routine_poll else LOG.info
        request_log(
            "request started: target=ComfyUI method=%s path=%s",
            request.method,
            request.path,
        )
        route_kind = classify_comfy_route(request.method, request.path)
        upstream_raw_path = comfy_upstream_path(request.rel_url.raw_path)
        if route_kind is ComfyRouteKind.LIFECYCLE:
            LOG.warning(
                "request rejected: target=ComfyUI method=%s path=%s status=403 "
                "reason=broker-managed lifecycle endpoint duration=%.3fs",
                request.method,
                request.path,
                time.monotonic() - started,
            )
            return web.json_response(
                {"error": "ComfyUI lifecycle endpoints are managed by the broker"},
                status=403,
            )
        if route_kind is ComfyRouteKind.BLOCKED_GPU:
            LOG.warning(
                "request rejected: target=ComfyUI method=%s path=%s status=403 "
                "reason=uncoordinated GPU route duration=%.3fs",
                request.method,
                request.path,
                time.monotonic() - started,
            )
            return web.json_response(
                {
                    "error": (
                        "ComfyUI route blocked because it can start GPU work outside "
                        "the workflow coordinator"
                    )
                },
                status=403,
            )
        if (
            route_kind is ComfyRouteKind.UNKNOWN_MUTATION
            and not self.config.allow_unknown_comfy_routes
        ):
            LOG.warning(
                "request rejected: target=ComfyUI method=%s path=%s status=403 "
                "reason=unclassified mutating route duration=%.3fs",
                request.method,
                request.path,
                time.monotonic() - started,
            )
            return web.json_response(
                {
                    "error": (
                        "unclassified mutating ComfyUI route rejected by strict proxy policy"
                    )
                },
                status=403,
            )
        if route_kind is ComfyRouteKind.WORKFLOW:
            try:
                body = await _read_workflow_body(
                    request,
                    self.config.max_workflow_body_bytes,
                )
                headers = dict(
                    upstream_request_headers(request, self.config.comfy_url).items()
                )
                response = await self._submit_image_until_disconnect(
                    request,
                    path=upstream_raw_path,
                    query_string=request.rel_url.raw_query_string,
                    headers=headers,
                    body=body,
                )
                LOG.info(
                    "request completed: target=ComfyUI method=%s path=%s status=%s "
                    "duration=%.3fs",
                    request.method,
                    request.path,
                    response.status,
                    time.monotonic() - started,
                )
                return response.to_web_response()
            except WorkflowBodyTooLarge:
                return web.json_response(
                    {"error": "workflow request body exceeds the configured size limit"},
                    status=413,
                )
            except QueueFull as exc:
                LOG.warning(
                    "request rejected: target=ComfyUI method=%s path=%s status=429 "
                    "reason=%s duration=%.3fs",
                    request.method,
                    request.path,
                    exc,
                    time.monotonic() - started,
                )
                return web.json_response(
                    {"error": str(exc)},
                    status=429,
                    headers={"Retry-After": "1"},
                )
            except BrokerError as exc:
                LOG.error(
                    "request rejected: target=ComfyUI method=%s path=%s status=503 "
                    "reason=%s duration=%.3fs",
                    request.method,
                    request.path,
                    exc,
                    time.monotonic() - started,
                )
                return web.json_response({"error": str(exc)}, status=503)
        try:
            is_websocket = request.headers.get("Upgrade", "").lower() == "websocket"
            buffered_body: bytes | None = None
            if request.method == "POST" and request.path == "/history":
                buffered_body = await request.read()
                prompt_ids = _history_delete_prompt_ids(buffered_body)
                if prompt_ids:
                    await self.coordinator.wait_before_history_delete(prompt_ids)
            if is_websocket:
                response = await proxy_websocket(
                    request,
                    self.websocket_session,
                    self.config.comfy_url,
                )
            elif route_kind is ComfyRouteKind.CONTROL:
                async with self.coordinator.comfy_control_lease():
                    response = await proxy_stream(
                        request,
                        self.control_session,
                        self.config.comfy_url,
                        raw_path=upstream_raw_path,
                        target="ComfyUI",
                    )
            else:
                response = await proxy_stream(
                    request,
                    self.image_session,
                    self.config.comfy_url,
                    raw_path=upstream_raw_path,
                    target="ComfyUI",
                    body=buffered_body,
                )
            request_log(
                "request completed: target=ComfyUI method=%s path=%s status=%s duration=%.3fs",
                request.method,
                request.path,
                response.status,
                time.monotonic() - started,
            )
            return response
        except HandoffError as exc:
            LOG.warning(
                "request rejected: target=ComfyUI method=%s path=%s status=409 reason=%s "
                "duration=%.3fs",
                request.method,
                request.path,
                exc,
                time.monotonic() - started,
            )
            return web.json_response({"error": str(exc)}, status=409)
        except (BrokerError, ClientError, TimeoutError) as exc:
            LOG.error(
                "request failed: target=ComfyUI method=%s path=%s status=502 error=%s "
                "duration=%.3fs",
                request.method,
                request.path,
                type(exc).__name__,
                time.monotonic() - started,
            )
            return web.json_response({"error": "ComfyUI upstream request failed"}, status=502)

    async def stop(self) -> None:
        if self.media_jobs is not None:
            self.media_jobs.stop_accepting()
        if self.coordinator is not None:
            await self.coordinator.close()
            self.coordinator = None
        if self.media_jobs is not None:
            await self.media_jobs.close()
            self.media_jobs = None
        if self.mcp_listener is not None:
            await self.mcp_listener.close()
            self.mcp_listener = None
        if self._image_runner is not None:
            await self._image_runner.cleanup()
            self._image_runner = None
        if self._chat_runner is not None:
            await self._chat_runner.cleanup()
            self._chat_runner = None
        for attribute in (
            "websocket_session",
            "image_session",
            "chat_session",
            "control_session",
        ):
            session = getattr(self, attribute)
            if session is not None:
                await session.close()
                setattr(self, attribute, None)
        self.llm = None
        LOG.info("broker stopped")
