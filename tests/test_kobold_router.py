from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from aiohttp import ClientSession, web

from st_proxy.cli import check_backend
from st_proxy.config import BrokerConfig, TestEndpointRegistry
from st_proxy.errors import UpstreamError
from st_proxy.service import BrokerService

from .support import DynamicServer, MockComfy, MockKobold, prompt_payload, wait_until


class NativeRouter(MockKobold):
    """Selects models in the upstream, with the native router's reload lock.

    The lock ends before streaming: the broker must protect the full response.
    Only Content-Length bodies are accepted, like KoboldCpp v1.120's router.
    """

    def __init__(self):
        super().__init__()
        self.router_enabled = True
        self.lock = asyncio.Lock()
        self.selected = []
        self.bodies = []
        self.lengths = []
        self.fail_load = False
        self.load_gate = asyncio.Event()
        self.load_gate.set()
        self.load_started = asyncio.Event()
        self.profiles = ["a.kcpps", "b.kcpps"]
        self.profile_models = {}
        self.loads = []
        self.current_profile = "initial_model"

    async def version(self, request):
        async with self.lock:
            response = await super().version(request)
            payload = json.loads(response.body)
            payload["router"] = self.router_enabled
            return web.json_response(payload)

    async def get_model(self, request):
        async with self.lock:
            return await super().get_model(request)

    async def admin(self, request):
        async with self.lock:
            response = await super().admin(request)
            if self.model == "inactive":
                self.current_profile = "unload_model"
            return response

    async def get_models(self, request):
        async with self.lock:
            self.metadata_paths.append(request.path)
            return web.json_response(
                {
                    "object": "list",
                    "data": [
                        {"id": name, "status": {"value": "loaded"}}
                        for name in [self.model, *self.profiles, "initial_model", "unload_model"]
                    ],
                }
            )

    async def select(self, request):
        async with self.lock:
            assert request.content_length is not None
            assert "Transfer-Encoding" not in request.headers
            body = await request.read()
            assert len(body) == request.content_length
            self.bodies.append(body)
            self.lengths.append(request.content_length)
            selected = json.loads(body)["model"]
            if selected != self.current_profile:
                self.load_started.set()
                await self.load_gate.wait()
                if self.fail_load:
                    return web.json_response({"error": "load failed"}, status=500)
                self.loads.append(selected)
                self.current_profile = selected
            self.selected.append(selected)
            self.events.append(f"selected:{selected}")
            self.model = self.profile_models.get(selected, selected)
        return None

    async def stream(self, request):
        failure = await self.select(request)
        if failure is not None:
            return failure
        return await super().stream(request)

    async def generate(self, request):
        failure = await self.select(request)
        if failure is not None:
            return failure
        return await super().generate(request)

    async def generic(self, request):
        if request.path == "/api/extra/abort":
            assert request.content_length is not None
            await request.read()
            self.events.append("abort")
            self.chat_release.set()
            return web.json_response({"success": True})
        if request.path in {"/v1/chat/completions", "/v1/completions"}:
            return await self.stream(request)
        if request.path == "/api/extra/tokencount":
            failure = await self.select(request)
            if failure is not None:
                return failure
            return web.json_response({"value": 7, "model": self.model})
        return await super().generic(request)


class RouterBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="router-test-")
        self.cache = Path(self.directory.name) / "models.json"
        self.cache.write_text(
            json.dumps(
                {
                    "data": [
                        {"id": "initial_model"},
                        {"id": "a.kcpps"},
                        {"id": "b.kcpps"},
                        {"id": "inactive"},
                        {"id": "unload_model"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.router = NativeRouter()
        self.comfy = MockComfy()
        self.router_server = DynamicServer(self.router.app())
        self.comfy_server = DynamicServer(self.comfy.app())
        registry = TestEndpointRegistry()
        router_url = registry.approve_disposable(await self.router_server.start())
        comfy_url = registry.approve_disposable(await self.comfy_server.start())
        self.config = BrokerConfig.for_test(
            registry=registry,
            llm_url=router_url,
            comfy_url=comfy_url,
            kobold_router_mode=True,
            kobold_model_cache=str(self.cache),
        )
        self.service = BrokerService(self.config)
        await self.service.start()
        self.client = ClientSession()

    async def asyncTearDown(self):
        self.router.load_gate.set()
        self.router.chat_release.set()
        for event in self.comfy.completion.values():
            event.set()
        await self.client.close()
        await self.service.stop()
        await self.router_server.close()
        await self.comfy_server.close()
        self.directory.cleanup()

    @property
    def chat(self):
        return f"http://127.0.0.1:{self.service.chat_port}"

    @property
    def image(self):
        return f"http://127.0.0.1:{self.service.image_port}"

    def status(self):
        return self.service.coordinator.status()

    async def generate(self, model="b.kcpps"):
        async with self.client.post(
            self.chat + "/api/v1/generate",
            json={"model": model, "prompt": "synthetic"},
        ) as response:
            await response.read()
            return response.status

    async def prompt(self):
        async with self.client.post(self.image + "/prompt", json=prompt_payload()) as response:
            await response.read()
            return response.status

    async def test_cached_discovery_is_passive_at_start_and_during_comfy(self):
        self.assertEqual(self.router.request_paths, [])
        for during_comfy in (False, True):
            if during_comfy:
                self.comfy.auto_complete = False
                self.assertEqual(await self.prompt(), 200)
            before = list(self.router.metadata_paths)
            frees = self.comfy.free_calls
            async with self.client.get(self.chat + "/v1/models") as response:
                payload = await response.json()
            self.assertEqual(
                [item["id"] for item in payload["data"]],
                [
                    "initial_model",
                    "a.kcpps",
                    "b.kcpps",
                ],
            )
            self.assertTrue(all("status" not in item for item in payload["data"]))
            self.assertEqual(before, self.router.metadata_paths)
            self.assertEqual(frees, self.comfy.free_calls)
        self.assertEqual(self.router.selected, [])

    async def test_router_selects_b_directly_after_comfy_and_defaults_missing_model(self):
        self.assertEqual(await self.prompt(), 200)
        await wait_until(lambda: self.status()["state"] == "comfy_ready")
        self.assertEqual(await self.generate(), 200)
        self.assertEqual(self.router.selected, ["b.kcpps"])
        self.assertEqual(self.router.admin_calls, ["unload_model"])
        self.assertEqual(self.comfy.free_calls, 1)
        self.assertEqual(self.status()["state"], "llm_reserved")
        async with self.client.post(self.chat + "/api/v1/generate", json={}) as response:
            self.assertEqual(response.status, 200)
            await response.read()
        self.assertEqual(self.router.selected[-1], "initial_model")

    async def test_chunked_unicode_body_and_tokenize_alias_reach_router(self):
        raw = '{"model":"b.kcpps","prompt":"räksmörgås"}'.encode()

        async def chunks():
            yield raw[:10]
            yield raw[10:]

        async with self.client.post(self.chat + "/api/extra/tokenize", data=chunks()) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["model"], "b.kcpps")
        self.assertEqual(self.router.bodies, [raw])
        self.assertEqual(self.router.lengths, [len(raw)])

    async def test_stream_holds_fifo_lease_and_abort_bypasses_queue(self):
        self.router.hold_chat = True
        stream = await self.client.post(
            self.chat + "/v1/chat/completions", json={"model": "a.kcpps"}
        )
        await self.router.chat_started.wait()
        image = asyncio.create_task(self.prompt())
        await wait_until(lambda: self.status()["state"] == "draining_llm")
        next_chat = asyncio.create_task(self.generate())
        await wait_until(lambda: self.status()["waiting_chats"] == 1)
        self.assertEqual(self.comfy.prompt_calls, [])
        self.assertEqual(self.router.selected, ["a.kcpps"])
        async with self.client.post(self.chat + "/api/extra/abort", json={}) as response:
            self.assertEqual(response.status, 200)
        await stream.read()
        stream.close()
        self.assertEqual(await image, 200)
        self.assertEqual(await next_chat, 200)
        self.assertLess(
            self.router.events.index("chat_finished"), self.router.events.index("unload_model")
        )
        self.assertEqual(self.router.selected, ["a.kcpps", "b.kcpps"])

    async def test_second_model_request_waits_for_full_stream(self):
        self.router.hold_chat = True
        stream = await self.client.post(
            self.chat + "/v1/chat/completions", json={"model": "a.kcpps"}
        )
        second = asyncio.create_task(self.generate())
        await wait_until(lambda: self.status()["active_chats"] == 1)
        await asyncio.sleep(0.05)
        self.assertEqual(self.router.selected, ["a.kcpps"])
        self.router.chat_release.set()
        await stream.read()
        stream.close()
        self.assertEqual(await second, 200)
        self.assertLess(
            self.router.events.index("chat_finished"), self.router.events.index("selected:b.kcpps")
        )

    async def test_disconnected_stream_is_drained_before_comfy(self):
        self.router.hold_chat = True
        stream = await self.client.post(
            self.chat + "/v1/chat/completions", json={"model": "a.kcpps"}
        )
        await stream.content.read(1)
        stream.close()
        image = asyncio.create_task(self.prompt())
        await wait_until(lambda: self.status()["state"] == "draining_llm")
        self.assertEqual(self.comfy.prompt_calls, [])
        self.router.chat_release.set()
        self.assertEqual(await image, 200)

    async def test_cleanup_failure_prevents_router_loading(self):
        self.comfy.cleanup_failures = 1
        self.assertEqual(await self.generate(), 503)
        self.assertEqual(self.router.selected, [])
        self.assertEqual(await self.generate(), 200)

    async def test_native_router_reuses_profile_and_switches_same_gguf_profiles(self):
        self.router.profile_models = {"a.kcpps": "same.gguf", "b.kcpps": "same.gguf"}
        self.assertEqual(await self.generate("a.kcpps"), 200)
        self.assertEqual(await self.generate("a.kcpps"), 200)
        self.assertEqual(await self.generate("b.kcpps"), 200)
        self.assertEqual(self.router.loads, ["a.kcpps", "b.kcpps"])
        self.assertEqual(self.router.admin_calls, [])

    async def test_disconnected_waiting_client_never_reaches_router(self):
        self.router.hold_chat = True
        stream = await self.client.post(
            self.chat + "/v1/chat/completions",
            json={"model": "a.kcpps"},
        )
        _reader, writer = await asyncio.open_connection("127.0.0.1", self.service.chat_port)
        body = b'{"model":"b.kcpps"}'
        writer.write(
            b"POST /api/v1/generate HTTP/1.1\r\nHost: localhost\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.1)
        self.router.chat_release.set()
        await stream.read()
        stream.close()
        await wait_until(lambda: not self.status()["active_chats"])
        self.assertEqual(await self.generate("a.kcpps"), 200)
        self.assertEqual(self.router.selected, ["a.kcpps", "a.kcpps"])

    async def test_load_failure_requires_confirmed_unload_before_comfy(self):
        self.router.fail_load = True
        self.assertEqual(await self.generate(), 500)
        self.router.unload_failures = 1
        self.assertEqual(await self.prompt(), 503)
        self.assertEqual(self.comfy.prompt_calls, [])
        self.router.fail_load = False
        self.assertEqual(await self.generate(), 200)
        self.assertEqual(await self.prompt(), 200)

    async def test_timeout_during_reload_cannot_grant_comfy(self):
        await self.service.stop()
        self.config = replace(self.config, request_timeout=0.08, unload_timeout=0.15)
        self.service = BrokerService(self.config)
        await self.service.start()
        self.router.load_gate.clear()
        self.assertEqual(await self.generate(), 502)
        self.assertEqual(await self.prompt(), 503)
        self.assertEqual(self.comfy.prompt_calls, [])
        self.router.load_gate.set()
        await wait_until(lambda: bool(self.router.selected))
        self.assertEqual(await self.prompt(), 200)

    async def test_size_limit_and_invalid_json_never_reach_router(self):
        await self.service.stop()
        self.service = BrokerService(replace(self.config, max_chat_body_bytes=40))
        await self.service.start()
        async with self.client.post(self.chat + "/api/v1/generate", data=b"x" * 41) as response:
            self.assertEqual(response.status, 413)
        async with self.client.post(self.chat + "/api/v1/generate", data=b"[]") as response:
            self.assertEqual(response.status, 400)
        self.assertEqual(self.router.selected, [])

    async def test_native_capability_validation_and_cache_export(self):
        destination = str(Path(self.directory.name) / "export.json")
        await check_backend(self.config, 1, destination)
        self.assertEqual(len(json.loads(Path(destination).read_text())["data"]), 3)
        self.router.router_enabled = False
        with self.assertRaises(UpstreamError):
            await check_backend(self.config, 1)
        self.assertEqual(self.router.admin_calls, [])

    async def test_connection_metadata_never_acquires_gpu_and_refresh_is_passive(self):
        async with self.client.get(self.chat + "/api/extra/version") as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.comfy.free_calls, 0)
        self.assertEqual(self.status()["gpu_owner"], None)
        self.assertEqual(await self.generate(), 200)
        self.router.profiles.append("c.kcpps")
        async with self.client.get(self.chat + "/v1/models") as response:
            self.assertEqual(len((await response.json())["data"]), 4)
        self.assertEqual(self.router.selected, ["b.kcpps"])
