from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector, web

from .comfy import ComfyClient
from .comfy_routes import ComfyRouteKind, classify_comfy_route, comfy_upstream_path
from .config import BrokerConfig
from .coordinator import HandoffCoordinator
from .errors import BrokerError, ChatUnavailable, HandoffError, QueueFull
from .http import BufferedResponse, proxy_stream, proxy_websocket, upstream_request_headers
from .kobold_router import ABORT_PATH, TEXT_PATHS, default_model_body, read_model_cache
from .llm import LlmBackend, build_llm_backend
from .llm.koboldcpp import KoboldCppBackend

LOG = logging.getLogger(__name__)
STATUS_PATH = "/broker/status"
CONTROL_CONNECTION_LIMIT = 16
CHAT_CONNECTION_LIMIT = 64
IMAGE_CONNECTION_LIMIT = 64
WEBSOCKET_CONNECTION_LIMIT = 32


class WorkflowBodyTooLarge(ValueError):
    """Raised before a workflow body can grow beyond its configured limit."""


def _is_routine_comfy_poll(request: web.Request) -> bool:
    return request.method == "GET" and (
        request.path == "/history"
        or request.path.startswith("/history/")
        or request.path == "/api/jobs"
        or request.path.endswith("/jobs/status")
    )


def _is_passive_llm_metadata(request: web.Request, backend_kind: str) -> bool:
    if backend_kind == "tabbyapi":
        return request.method in {"GET", "HEAD"} and request.path.rstrip("/") in {
            "/v1/models", "/v1/model/list", "/v1/model", "/health", "/props",
        }
    return (
        backend_kind == "koboldcpp"
        and request.method == "GET"
        and request.path in {"/api/v1/model", "/v1/models"}
    )


def _inactive_llm_metadata(path: str, backend_kind: str = "koboldcpp") -> web.Response:
    if backend_kind == "tabbyapi":
        if path.rstrip("/") in {"/v1/models", "/v1/model/list"}:
            return web.json_response({"object": "list", "data": []})
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
        self.control_session: ClientSession | None = None
        self.chat_session: ClientSession | None = None
        self.image_session: ClientSession | None = None
        self.websocket_session: ClientSession | None = None
        self.llm: LlmBackend[Any] | None = None
        self.coordinator: HandoffCoordinator | None = None
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
            if self.config.kobold_router_mode:
                response = await self._router_handler(request)
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
                        )
                    else:
                        response = _inactive_llm_metadata(request.path, self.llm.info.kind)
            else:
                async with self.coordinator.chat_lease():
                    response = await proxy_stream(
                        request,
                        self.chat_session,
                        self.llm.info.chat_origin,
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
            LOG.warning(
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
            LOG.warning(
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

    async def _router_body(self, request: web.Request) -> bytes:
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
                            LOG.warning("router model discovery failed; retaining cached list")
                    return web.json_response(self._router_models)
                if not available:
                    return _inactive_llm_metadata(path)
                return await proxy_stream(request, self.chat_session, self.llm.info.chat_origin)
        if path.startswith("/api/admin/reload_config") or path == "/noscript":
            raise web.HTTPForbidden(text="model lifecycle is owned by the broker")
        abort = request.method == "POST" and path == ABORT_PATH
        if request.method in {"GET", "HEAD", "OPTIONS"} or abort:
            async with self.coordinator.router_control(abort=abort) as available:
                if not available:
                    raise ChatUnavailable("router control is unavailable during GPU transition")
                body = await self._router_body(request) if request.can_read_body else b""
                return await proxy_stream(
                    request, self.chat_session, self.llm.info.chat_origin, body=body,
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
            body = await self._router_body(request)
            path = request.path.rstrip("/")
            if request.method == "POST" and path in TEXT_PATHS:
                try:
                    body = default_model_body(body)
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
                LOG.warning(
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
                    )
            else:
                response = await proxy_stream(
                    request,
                    self.image_session,
                    self.config.comfy_url,
                    raw_path=upstream_raw_path,
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
            LOG.warning(
                "request failed: target=ComfyUI method=%s path=%s status=502 error=%s "
                "duration=%.3fs",
                request.method,
                request.path,
                type(exc).__name__,
                time.monotonic() - started,
            )
            return web.json_response({"error": "ComfyUI upstream request failed"}, status=502)

    async def stop(self) -> None:
        if self.coordinator is not None:
            await self.coordinator.close()
            self.coordinator = None
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
