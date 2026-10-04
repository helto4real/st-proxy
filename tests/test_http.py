import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientPayloadError
from aiohttp.test_utils import make_mocked_request

from st_proxy.http import proxy_stream, upstream_url
from st_proxy.llm.prism_diagnostics import PrismDiagnostics

from .test_prism_diagnostics import LOGGER, stream_bytes


def test_upstream_url_preserves_raw_path_and_query_encoding() -> None:
    request = make_mocked_request(
        "GET",
        "/api/userdata/workflows%2Fsynthetic.json?label=synthetic%20workflow",
    )

    assert upstream_url("http://127.0.0.1:8188", request) == (
        "http://127.0.0.1:8188/api/userdata/workflows%2Fsynthetic.json?label=synthetic%20workflow"
    )
    assert upstream_url(
        "http://127.0.0.1:8188",
        request,
        raw_path="/userdata/workflows%2Fsynthetic.json",
    ) == ("http://127.0.0.1:8188/userdata/workflows%2Fsynthetic.json?label=synthetic%20workflow")


class StreamObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_drains_and_upstream_failure_is_reported(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                drained = []

                async def chunks(interrupted=interrupted, drained=drained):
                    yield stream_bytes()
                    if interrupted:
                        raise ClientPayloadError("synthetic truncated response")
                    drained.append(True)

                session = MagicMock()
                session.request.return_value.__aenter__.return_value = SimpleNamespace(
                    status=200, reason="OK", content_type="text/event-stream",
                    headers={"Content-Type": "text/event-stream"},
                    content=SimpleNamespace(iter_any=chunks),
                )
                downstream = MagicMock()
                downstream.prepare = AsyncMock()
                downstream.write = AsyncMock(side_effect=ConnectionResetError())
                downstream.write_eof = AsyncMock()
                observer = PrismDiagnostics(b'{"max_tokens":1500}')
                request = make_mocked_request("POST", "/v1/chat/completions")
                with patch("st_proxy.http.web.StreamResponse", return_value=downstream):
                    with self.assertLogs(LOGGER, "INFO") as captured:
                        if interrupted:
                            with self.assertRaises(ClientPayloadError):
                                await proxy_stream(request, session, "http://upstream",
                                                   body=b"{}", observer=observer)
                        else:
                            await proxy_stream(request, session, "http://upstream",
                                               body=b"{}", observer=observer)
                self.assertEqual(drained, [] if interrupted else [True])
                downstream.write_eof.assert_not_awaited()
                state = "interrupted" if interrupted else "complete"
                self.assertIn(f"stream_status={state} client_disconnected=true", captured.output[0])
