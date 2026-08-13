from __future__ import annotations

import asyncio
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
from .llm import LlmBackend, build_llm_backend

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
        request.path == "/history" or request.path.startswith("/history/")
    )


def _is_passive_llm_metadata(request: web.Request, backend_kind: str) -> bool:
    return (
        backend_kind == "koboldcpp"
        and request.method == "GET"
        and request.path in {"/api/v1/model", "/v1/models"}
    )


def _inactive_llm_metadata(path: str) -> web.Response:
    if path == "/v1/models":
        return web.json_response({"object": "list", "data": []})
    return web.json_response({"result": "inactive"})


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
            if _is_passive_llm_metadata(request, self.llm.info.kind):
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
                        response = _inactive_llm_metadata(request.path)
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
