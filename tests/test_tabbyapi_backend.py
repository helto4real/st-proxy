from __future__ import annotations

import asyncio
import unittest

from aiohttp import ClientSession

from st_proxy.cli import check_backend
from st_proxy.config import BrokerConfig, TestEndpointRegistry
from st_proxy.errors import ConfigurationError, UpstreamError
from st_proxy.llm.base import BackendTimeouts
from st_proxy.llm.tabbyapi import TabbyApiBackend
from st_proxy.service import BrokerService

from .support import DynamicServer, MockComfy, prompt_payload, wait_until
from .tabby_support import MockTabby


class TabbyTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tabby = MockTabby()
        self.comfy = MockComfy()
        self.server = DynamicServer(self.tabby.app())
        self.comfy_server = DynamicServer(self.comfy.app())
        origin = await self.server.start()
        comfy_origin = await self.comfy_server.start()
        self.client = ClientSession()
        self.backend = TabbyApiBackend(
            self.client, origin=origin, model="synthetic-exl3", max_seq_len=32768,
            timeouts=BackendTimeouts(1, 1, 1, 0.01),
        )
        registry = TestEndpointRegistry()
        registry.approve_disposable(origin)
        registry.approve_disposable(comfy_origin)
        self.config = BrokerConfig.for_test(
            llm_backend="tabbyapi", llm_url=origin, comfy_url=comfy_origin,
            registry=registry, tabby_model="synthetic-exl3",
        )
        self.service = BrokerService(self.config)
        await self.service.start()
        self.chat_url = f"http://127.0.0.1:{self.service.chat_port}"
        self.image_url = f"http://127.0.0.1:{self.service.image_port}"

    async def asyncTearDown(self):
        self.tabby.chat_release.set()
        self.tabby.load_release.set()
        for event in self.comfy.completion.values():
            event.set()
        await self.service.stop()
        await self.client.close()
        await self.server.close()
        await self.comfy_server.close()

    async def chat(self, **payload):
        return await self.client.post(f"{self.chat_url}/v1/chat/completions", json=payload)

    async def image(self):
        return await self.client.post(f"{self.image_url}/prompt", json=prompt_payload())

    async def comfy_ready(self):
        await wait_until(lambda: self.service.coordinator.status()["state"] == "comfy_ready")

    async def test_adapter_roundtrip_preserves_model_context_and_cache(self):
        await self.backend.validate_control()
        target = await self.backend.snapshot_ready()
        await self.backend.release_gpu(target)
        self.assertIsNone(await self.backend.observe_ready())
        restored = await self.backend.acquire_gpu(target)
        self.assertEqual(restored, target)
        self.assertEqual(self.tabby.loads, [{
            "model_name": "synthetic-exl3", "backend": "exllamav3", "skip_queue": False,
            "max_seq_len": 32768, "cache_size": 65536,
            "cache_mode": "Q4", "chunk_size": 2048, "vision": False,
        }])

    async def test_release_captures_native_model_change(self):
        target = await self.backend.snapshot_ready()
        self.tabby.model = "another-exl3"
        self.tabby.parameters["cache_mode"] = "Q6"
        await self.backend.release_gpu(target)
        await self.backend.acquire_gpu(target)
        self.assertEqual(self.tabby.loads[0]["model_name"], "another-exl3")
        self.assertEqual(self.tabby.loads[0]["cache_mode"], "Q6")

    async def test_initialization_and_check_are_passive_when_unloaded(self):
        self.assertEqual(self.tabby.request_paths, [])
        self.assertEqual(self.comfy.free_calls, 0)
        self.tabby.model = "inactive"
        await check_backend(self.config, 1)
        self.assertEqual(self.tabby.loads, [])
        self.assertEqual(self.tabby.unloads, 0)
        self.assertEqual(self.comfy.free_calls, 0)

    async def test_cold_load_uses_configured_context(self):
        self.tabby.model = "inactive"
        self.tabby.parameters["max_seq_len"] = 4096
        response = await self.chat(model="synthetic-exl3")
        self.assertEqual(response.status, 200)
        response.close()
        self.assertEqual(self.tabby.loads[0]["max_seq_len"], 32768)
        self.assertEqual(self.comfy.free_calls, 1)

    async def test_finished_sse_does_not_release_before_readiness(self):
        self.tabby.model = "inactive"
        self.tabby.hold_load = True
        chat = asyncio.create_task(self.chat())
        await self.tabby.load_finished.wait()
        image = asyncio.create_task(self.image())
        await wait_until(lambda: self.service.coordinator.status()["waiting_images"] == 1)
        self.assertFalse(chat.done())
        self.assertEqual(self.tabby.chat_requests, 0)
        self.assertEqual(self.comfy.prompt_calls, [])
        self.tabby.load_release.set()
        (await chat).close()
        (await image).close()
        self.assertEqual(self.tabby.unloads, 1)

    async def test_sse_error_after_finished_keeps_handoff_blocked(self):
        self.tabby.model = "inactive"
        self.tabby.load_error = True
        response = await self.chat()
        self.assertEqual(response.status, 503)
        response.close()
        for _ in range(2):
            response = await self.image()
            self.assertGreaterEqual(response.status, 400)
            response.close()
        self.assertEqual(self.comfy.prompt_calls, [])
        self.assertEqual(len(self.tabby.loads), 1)

    async def test_load_timeout_does_not_mean_unloaded(self):
        self.backend._timeouts = BackendTimeouts(1, 1, 0.05, 0.01)
        self.tabby.model = "inactive"
        self.tabby.hold_load = True
        with self.assertRaises(UpstreamError):
            await self.backend.acquire_gpu(None)
        with self.assertRaises(UpstreamError):
            await self.backend.observe_ready()
        self.tabby.load_release.set()
        await wait_until(lambda: self.tabby.model != "inactive")
        with self.assertRaises(UpstreamError):
            await self.backend.observe_ready()

    async def test_wrong_context_is_not_ready(self):
        self.tabby.model = "inactive"
        self.tabby.wrong_context = True
        with self.assertRaisesRegex(UpstreamError, "mismatch"):
            await self.backend.acquire_gpu(None)

    async def test_passive_routes_do_not_take_gpu_from_comfy(self):
        (await self.image()).close()
        await self.comfy_ready()
        for path in ("/v1/models", "/v1/model/list", "/v1/model", "/health", "/props"):
            async with self.client.get(self.chat_url + path) as response:
                self.assertIn(response.status, (200, 503))
        self.assertEqual(self.tabby.loads, [])
        self.assertEqual(self.comfy.free_calls, 0)
        self.assertEqual(self.service.coordinator.status()["gpu_owner"], "comfy")

    async def test_request_and_response_are_transparent_across_handoff(self):
        raw = (b'{"model":"native-name","temperature":0.71,"reasoning_effort":"low",'
               b'"chat_template_kwargs":{"enable_thinking":false},"unknown":42}')
        for run in range(2):
            async with self.client.post(
                f"{self.chat_url}/v1/chat/completions", data=raw,
                headers={"Content-Type": "application/json"},
            ) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())["choices"][0]["message"], {
                    "content": "synthetic", "reasoning_content": "synthetic reasoning",
                })
            if not run:
                (await self.image()).close()
                await self.comfy_ready()
        self.assertEqual(self.tabby.payloads, [raw, raw])
        self.assertEqual(self.tabby.events, ["chat", "unload", "load", "chat"])

    async def test_disconnected_stream_drains_before_handoff(self):
        self.tabby.hold_chat = True
        response = await self.chat(stream=True)
        self.assertEqual(await response.content.readline(), b"data: synthetic-one\n")
        response.close()
        image = asyncio.create_task(self.image())
        await wait_until(lambda: self.service.coordinator.status()["state"] == "draining_llm")
        self.assertEqual(self.tabby.unloads, 0)
        self.tabby.chat_release.set()
        (await image).close()
        self.assertLess(self.tabby.events.index("chat_finished"), self.tabby.events.index("unload"))

    async def test_failed_unload_never_submits_comfy(self):
        self.tabby.unload_failures = 1
        for _ in range(2):
            response = await self.image()
            self.assertGreaterEqual(response.status, 400)
            response.close()
        self.assertEqual(self.comfy.prompt_calls, [])
        self.assertIsNone(self.service.coordinator.status()["gpu_owner"])

    async def test_failed_comfy_cleanup_can_recover_on_next_chat(self):
        (await self.image()).close()
        await self.comfy_ready()
        self.comfy.cleanup_failures = 1
        response = await self.chat()
        self.assertEqual(response.status, 503)
        response.close()
        self.assertEqual(self.tabby.loads, [])
        response = await self.chat()
        self.assertEqual(response.status, 200)
        response.close()
        self.assertEqual(len(self.tabby.loads), 1)

    async def test_cancelled_queued_chat_is_removed(self):
        self.comfy.auto_complete = False
        (await self.image()).close()
        coordinator = self.service.coordinator

        async def queued_chat():
            async with coordinator.chat_lease():
                self.fail("cancelled chat must not acquire a lease")

        task = asyncio.create_task(queued_chat())
        await wait_until(lambda: coordinator.status()["waiting_chats"] == 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(coordinator.status()["waiting_chats"], 0)
        self.comfy.completion["synthetic-1"].set()
        await self.comfy_ready()
        self.assertEqual(self.tabby.loads, [])

    async def test_context_mismatch_in_existing_model_fails_passively(self):
        self.tabby.parameters["max_seq_len"] = 4096
        with self.assertRaises(UpstreamError):
            await self.backend.observe_ready()
        self.assertEqual(self.tabby.loads, [])
        self.assertEqual(self.tabby.unloads, 0)

    async def test_test_mode_rejects_tabby_production_port(self):
        with self.assertRaises(ConfigurationError):
            TestEndpointRegistry().approve_disposable("http://127.0.0.1:5003")
