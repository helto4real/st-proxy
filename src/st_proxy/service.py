from __future__ import annotations

import logging
import time
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector, web

from .comfy import ComfyClient
from .comfy_routes import ComfyRouteKind, classify_comfy_route, comfy_upstream_path
from .config import BrokerConfig
from .coordinator import HandoffCoordinator
from .errors import BrokerError, ChatUnavailable, HandoffError
from .http import proxy_stream, proxy_websocket, upstream_request_headers
from .llm import LlmBackend, build_llm_backend

LOG = logging.getLogger(__name__)
STATUS_PATH = "/broker/status"


def _is_routine_comfy_poll(request: web.Request) -> bool:
    return request.method == "GET" and (
        request.path == "/history" or request.path.startswith("/history/")
    )


def _bound_port(site: web.TCPSite) -> int:
    server = site._server  # aiohttp has no public bound-port accessor
    if server is None or not server.sockets:
        raise RuntimeError("site did not bind a socket")
    return int(server.sockets[0].getsockname()[1])


class BrokerService:
    def __init__(self, config: BrokerConfig) -> None:
        self.config = config
        self.session: ClientSession | None = None
        self.llm: LlmBackend[Any] | None = None
        self.coordinator: HandoffCoordinator | None = None
        self._chat_runner: web.AppRunner | None = None
        self._image_runner: web.AppRunner | None = None
        self._chat_site: web.TCPSite | None = None
        self._image_site: web.TCPSite | None = None
        self.chat_port: int | None = None
        self.image_port: int | None = None

    async def start(self) -> None:
        timeout = ClientTimeout(total=None, sock_connect=self.config.request_timeout)
        self.session = ClientSession(
            timeout=timeout,
            connector=TCPConnector(force_close=True),
            auto_decompress=False,
        )
        self.llm = build_llm_backend(self.session, self.config)
        self.coordinator = HandoffCoordinator(
            self.config,
            self.llm,
            ComfyClient(self.session, self.config),
        )
        await self.coordinator.initialize()
        chat_app = web.Application(client_max_size=1024**3)
        image_app = web.Application(client_max_size=1024**3)
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
        try:
            await self._chat_site.start()
            await self._image_site.start()
        except BaseException:
            await self.stop()
            raise
        self.chat_port = _bound_port(self._chat_site)
        self.image_port = _bound_port(self._image_site)

    async def _status(self) -> web.Response:
        assert self.coordinator is not None
        return web.json_response(self.coordinator.status())

    async def _chat_handler(self, request: web.Request) -> web.StreamResponse:
        assert self.coordinator is not None and self.session is not None
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
            async with self.coordinator.chat_lease():
                response = await proxy_stream(
                    request,
                    self.session,
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
        assert self.coordinator is not None and self.session is not None
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
        upstream_path = comfy_upstream_path(request.path)
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
                body = await request.read()
                headers = dict(
                    upstream_request_headers(request, self.config.comfy_url).items()
                )
                response = await self.coordinator.submit_image(
                    path=upstream_path,
                    query_string=request.query_string,
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
                response = await proxy_websocket(request, self.session, self.config.comfy_url)
            elif route_kind is ComfyRouteKind.CONTROL:
                async with self.coordinator.comfy_control_lease():
                    response = await proxy_stream(
                        request,
                        self.session,
                        self.config.comfy_url,
                        path=upstream_path,
                    )
            else:
                response = await proxy_stream(
                    request,
                    self.session,
                    self.config.comfy_url,
                    path=upstream_path,
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
        if self.session is not None:
            await self.session.close()
            self.session = None
        self.llm = None
        LOG.info("broker stopped")
