from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from aiohttp import (
    ClientError,
    ClientResponse,
    ClientSession,
    ClientWebSocketResponse,
    WSMsgType,
    web,
)
from multidict import CIMultiDict

HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

LOG = logging.getLogger(__name__)


def upstream_url(
    origin: str,
    request: web.Request,
    *,
    raw_path: str | None = None,
) -> str:
    base = urlsplit(origin)
    request_raw_path = request.rel_url.raw_path if raw_path is None else raw_path
    joined = f"{base.path.rstrip('/')}/{request_raw_path.lstrip('/')}"
    return urlunsplit((base.scheme, base.netloc, joined, request.rel_url.raw_query_string, ""))


def child_url(origin: str, path: str) -> str:
    base = urlsplit(origin)
    joined = f"{base.path.rstrip('/')}/{path.lstrip('/')}"
    return urlunsplit((base.scheme, base.netloc, joined, "", ""))


def websocket_url(origin: str, request: web.Request) -> str:
    target = urlsplit(upstream_url(origin, request))
    scheme = "wss" if target.scheme == "https" else "ws"
    return urlunsplit((scheme, target.netloc, target.path, target.query, ""))


def filtered_request_headers(request: web.Request) -> CIMultiDict[str]:
    result: CIMultiDict[str] = CIMultiDict()
    for key, value in request.headers.items():
        if key.lower() not in HOP_BY_HOP_HEADERS | {"host", "content-length"}:
            result.add(key, value)
    return result


def upstream_request_headers(request: web.Request, origin: str) -> CIMultiDict[str]:
    result = filtered_request_headers(request)
    upstream = urlsplit(origin)
    upstream_authority = urlunsplit((upstream.scheme, upstream.netloc, "", "", ""))
    downstream_authority = f"{request.scheme}://{request.host}"
    if "Origin" in result:
        result["Origin"] = upstream_authority
    if referer := result.get("Referer"):
        if referer.startswith(downstream_authority):
            result["Referer"] = upstream_authority + referer[len(downstream_authority) :]
    return result


def websocket_request_headers(request: web.Request, origin: str) -> CIMultiDict[str]:
    result = upstream_request_headers(request, origin)
    for key in tuple(result):
        if key.lower().startswith("sec-websocket-"):
            result.popall(key)
    return result


def filtered_response_headers(headers: Iterable[tuple[str, str]]) -> CIMultiDict[str]:
    result: CIMultiDict[str] = CIMultiDict()
    for key, value in headers:
        if key.lower() not in HOP_BY_HOP_HEADERS:
            result.add(key, value)
    return result


def downstream_response_headers(
    headers: Iterable[tuple[str, str]],
    *,
    origin: str,
    request: web.Request,
) -> CIMultiDict[str]:
    result = filtered_response_headers(headers)
    location = result.get("Location")
    if not location:
        return result
    upstream = urlsplit(origin)
    target = urlsplit(location)
    if target.scheme == upstream.scheme and target.netloc == upstream.netloc:
        result["Location"] = urlunsplit(
            (request.scheme, request.host, target.path, target.query, target.fragment)
        )
    return result


async def request_body(request: web.Request) -> AsyncIterator[bytes]:
    async for chunk in request.content.iter_chunked(64 * 1024):
        yield chunk


@dataclass(slots=True)
class BufferedResponse:
    status: int
    reason: str | None
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def to_web_response(self) -> web.Response:
        return web.Response(
            status=self.status,
            reason=self.reason,
            headers=filtered_response_headers(self.headers),
            body=self.body,
        )


async def buffer_response(response: ClientResponse) -> BufferedResponse:
    body = await response.read()
    return BufferedResponse(
        status=response.status,
        reason=response.reason,
        headers=tuple(response.headers.items()),
        body=body,
    )


async def proxy_stream(
    request: web.Request,
    session: ClientSession,
    origin: str,
    *,
    raw_path: str | None = None,
) -> web.StreamResponse:
    async with session.request(
        request.method,
        upstream_url(origin, request, raw_path=raw_path),
        headers=upstream_request_headers(request, origin),
        data=request_body(request),
        allow_redirects=False,
    ) as upstream:
        downstream = web.StreamResponse(
            status=upstream.status,
            reason=upstream.reason,
            headers=downstream_response_headers(
                upstream.headers.items(),
                origin=origin,
                request=request,
            ),
        )
        connected = True
        try:
            await downstream.prepare(request)
        except (ConnectionError, RuntimeError):
            connected = False
        async for chunk in upstream.content.iter_any():
            if connected:
                try:
                    await downstream.write(chunk)
                except (ConnectionError, RuntimeError):
                    # Drain the upstream generation before releasing the chat lease.
                    connected = False
        if connected:
            try:
                await downstream.write_eof()
            except (ConnectionError, RuntimeError):
                pass
        return downstream


async def _client_to_upstream(
    downstream: web.WebSocketResponse,
    upstream: ClientWebSocketResponse,
) -> None:
    async for message in downstream:
        if message.type is WSMsgType.TEXT:
            await upstream.send_str(message.data)
        elif message.type is WSMsgType.BINARY:
            await upstream.send_bytes(message.data)
        elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
            break


async def _upstream_to_client(
    upstream: ClientWebSocketResponse,
    downstream: web.WebSocketResponse,
) -> None:
    async for message in upstream:
        if message.type is WSMsgType.TEXT:
            await downstream.send_str(message.data)
        elif message.type is WSMsgType.BINARY:
            await downstream.send_bytes(message.data)
        elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
            break


async def proxy_websocket(
    request: web.Request,
    session: ClientSession,
    origin: str,
) -> web.WebSocketResponse:
    protocols = tuple(
        protocol.strip()
        for protocol in request.headers.get("Sec-WebSocket-Protocol", "").split(",")
        if protocol.strip()
    )
    async with session.ws_connect(
        websocket_url(origin, request),
        headers=websocket_request_headers(request, origin),
        protocols=protocols,
        max_msg_size=1024**3,
    ) as upstream:
        selected_protocol = (upstream.protocol,) if upstream.protocol else ()
        downstream = web.WebSocketResponse(
            protocols=selected_protocol,
            max_msg_size=1024**3,
        )
        await downstream.prepare(request)
        to_upstream = asyncio.create_task(
            _client_to_upstream(downstream, upstream),
            name="st-proxy-websocket-client-to-upstream",
        )
        to_client = asyncio.create_task(
            _upstream_to_client(upstream, downstream),
            name="st-proxy-websocket-upstream-to-client",
        )
        tasks = {to_upstream, to_client}
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            try:
                task.result()
            except (ClientError, ConnectionError, RuntimeError) as exc:
                LOG.debug("WebSocket relay ended after transport error: %s", type(exc).__name__)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        with contextlib.suppress(ConnectionError, RuntimeError):
            await downstream.close(code=upstream.close_code or 1000)
        return downstream
