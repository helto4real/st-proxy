from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from aiohttp import ClientResponse, ClientSession, web
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


def upstream_url(origin: str, request: web.Request) -> str:
    base = urlsplit(origin)
    path = f"{base.path.rstrip('/')}/{request.path.lstrip('/')}"
    return urlunsplit((base.scheme, base.netloc, path, request.query_string, ""))


def child_url(origin: str, path: str) -> str:
    base = urlsplit(origin)
    joined = f"{base.path.rstrip('/')}/{path.lstrip('/')}"
    return urlunsplit((base.scheme, base.netloc, joined, "", ""))


def filtered_request_headers(request: web.Request) -> CIMultiDict[str]:
    result: CIMultiDict[str] = CIMultiDict()
    for key, value in request.headers.items():
        if key.lower() not in HOP_BY_HOP_HEADERS | {"host", "content-length"}:
            result.add(key, value)
    return result


def filtered_response_headers(headers: Iterable[tuple[str, str]]) -> CIMultiDict[str]:
    result: CIMultiDict[str] = CIMultiDict()
    for key, value in headers:
        if key.lower() not in HOP_BY_HOP_HEADERS:
            result.add(key, value)
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
) -> web.StreamResponse:
    async with session.request(
        request.method,
        upstream_url(origin, request),
        headers=filtered_request_headers(request),
        data=request_body(request),
        allow_redirects=False,
    ) as upstream:
        downstream = web.StreamResponse(
            status=upstream.status,
            reason=upstream.reason,
            headers=filtered_response_headers(upstream.headers.items()),
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
