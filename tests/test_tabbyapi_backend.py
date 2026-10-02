from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

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
        self.tabby.parameters.update(max_seq_len=16384, cache_size=16384)
        await self.backend.release_gpu(target)
        await self.backend.acquire_gpu(target)
        self.assertEqual(self.tabby.loads[0]["model_name"], "another-exl3")
        self.assertEqual(self.tabby.loads[0]["cache_mode"], "Q6")
        self.assertEqual(self.tabby.loads[0]["max_seq_len"], 16384)
        self.assertEqual(self.tabby.loads[0]["cache_size"], 16384)

    async def test_initialization_and_check_are_passive_when_unloaded(self):
        self.assertEqual(self.tabby.request_paths, [])
        self.assertEqual(self.comfy.free_calls, 0)
        self.tabby.model = "inactive"
        await check_backend(self.config, 1)
        self.assertEqual(self.tabby.loads, [])
        self.assertEqual(self.tabby.unloads, 0)
        self.assertEqual(self.comfy.free_calls, 0)

    async def test_service_metadata_is_forwarded_before_first_active_request(self):
        self.tabby.model = "inactive"
        self.assertEqual(self.tabby.request_paths, [])
        expected_paths = []
        for path in ("/v1/models", "/v1/model/list", "/health"):
            for method in ("GET", "HEAD"):
                with self.subTest(path=path, method=method):
                    async with self.client.request(method, self.chat_url + path) as response:
                        self.assertEqual(response.status, 200)
                        if method == "HEAD":
                            self.assertEqual(await response.read(), b"")
                        elif path == "/health":
                            self.assertEqual(await response.json(), {
                                "status": "healthy", "issues": [],
                            })
                        else:
                            self.assertEqual(await response.json(), {
                                "object": "list", "data": [
                                    {"id": name, "object": "model"}
                                    for name in self.tabby.available_models
                                ],
                            })
                    expected_paths.append(path)
        self.assertEqual(self.tabby.request_paths, expected_paths)
        for path in ("/v1/model", "/props"):
            async with self.client.get(self.chat_url + path) as response:
                self.assertEqual(response.status, 503)
        self.assertEqual(self.tabby.request_paths, expected_paths)
        self.assertEqual(self.tabby.loads, [])
        self.assertEqual(self.tabby.unloads, 0)
        self.assertEqual(self.comfy.free_calls, 0)
        status = self.service.coordinator.status()
        self.assertEqual(status["state"], "awaiting_request")
        self.assertIsNone(status["gpu_owner"])
        self.assertEqual(status["active_chats"], 0)

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
        with self.assertRaises(UpstreamError):
            await self.backend.acquire_gpu(None)
        self.assertEqual(len(self.tabby.loads), 1)
        self.tabby.load_release.set()
        await wait_until(lambda: self.tabby.model != "inactive")
        observed = await self.backend.acquire_gpu(None)
        self.assertEqual(observed.model, "synthetic-exl3")
        self.assertEqual(len(self.tabby.loads), 1)
        self.assertEqual(self.tabby.unloads, 0)

    async def test_completed_load_failure_recovers_on_next_chat(self):
        (await self.image()).close()
        await self.comfy_ready()
        self.tabby.load_error = True
        for attempt in range(2):
            async with await self.chat() as response:
                self.assertEqual(response.status, 503)
            self.assertEqual(len(self.tabby.loads), attempt + 1)
        # Passive metadata and idle time must not trigger another attempt.
        async with self.client.get(self.chat_url + "/health") as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(len(self.tabby.loads), 2)
        self.tabby.load_error = False
        async with await self.chat() as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(len(self.tabby.loads), 3)
        self.assertEqual(self.tabby.loads[0], self.tabby.loads[2])
        self.assertEqual(self.service.coordinator.status()["state"], "llm_ready")

    async def test_failed_cold_load_can_recover_without_restore_point(self):
        self.tabby.model = "inactive"
        self.tabby.load_error = True
        async with await self.chat() as response:
            self.assertEqual(response.status, 503)
        self.tabby.load_error = False
        async with await self.chat() as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(len(self.tabby.loads), 2)
        self.assertEqual(self.tabby.loads[0], self.tabby.loads[1])

    async def test_vram_failure_is_logged_and_retries_only_on_next_active_request(self):
        for message in (
            "CUDA out of memory. private-model /private/path",
            "RuntimeError: Insufficient VRAM in split for model and cache",
            "CUDA error: out of memory",
            "torch.cuda.OutOfMemoryError: private details",
        ):
            with self.subTest(message=message):
                (await self.image()).close()
                await self.comfy_ready()
                previous_loads = len(self.tabby.loads)
                self.tabby.load_error = True
                self.tabby.load_error_message = message
                with self.assertLogs("st_proxy", level="ERROR") as captured:
                    async with await self.chat() as response:
                        self.assertEqual(response.status, 503)
                        self.assertIn("insufficient_vram", (await response.json())["error"])
                logs = "\n".join(captured.output)
                self.assertIn("not enough GPU memory", logs)
                self.assertNotIn(message, logs)
                self.assertNotIn("private", logs)
                status = self.service.coordinator.status()
                self.assertEqual(status["state"], "error")
                self.assertIsNone(status["gpu_owner"])
                self.assertIn("insufficient_vram", status["last_error"])
                self.assertEqual(len(self.tabby.loads), previous_loads + 1)
                async with self.client.get(self.chat_url + "/health") as response:
                    self.assertEqual(response.status, 200)
                self.assertEqual(len(self.tabby.loads), previous_loads + 1)

                # A second request retries even if memory is still exhausted.
                async with await self.chat() as response:
                    self.assertEqual(response.status, 503)
                self.assertEqual(len(self.tabby.loads), previous_loads + 2)
                self.tabby.load_error = False
                async with await self.chat() as response:
                    self.assertEqual(response.status, 200)
                self.assertEqual(len(self.tabby.loads), previous_loads + 3)
                self.assertEqual(self.tabby.loads[-1], self.tabby.loads[-3])
                self.assertEqual(self.service.coordinator.status()["state"], "llm_ready")

    async def test_vram_error_is_logged_before_stream_end_without_authorizing_retry(self):
        self.tabby.model = "inactive"
        self.tabby.load_error = True
        self.tabby.load_error_message = "CUDA out of memory"
        self.tabby.hold_error_stream = True
        self.backend._timeouts = BackendTimeouts(1, 1, 0.2, 0.01)
        with self.assertLogs("st_proxy", level="ERROR") as captured:
            load = asyncio.create_task(self.backend.acquire_gpu(None))
            await self.tabby.error_sent.wait()
            await wait_until(lambda: bool(captured.output))
            self.assertFalse(load.done())
            self.assertIn("insufficient_vram", "\n".join(captured.output))
            with self.assertRaises(UpstreamError):
                await load
        with self.assertRaises(UpstreamError):
            await self.backend.acquire_gpu(None)
        self.assertEqual(len(self.tabby.loads), 1)

    async def test_unknown_load_error_is_not_classified_as_vram(self):
        self.tabby.model = "inactive"
        self.tabby.load_error = True
        self.tabby.load_error_message = "invalid private-model at /private/path"
        with self.assertLogs("st_proxy", level="ERROR") as captured:
            async with await self.chat() as response:
                self.assertEqual(response.status, 503)
                self.assertNotIn("insufficient_vram", await response.text())
        logs = "\n".join(captured.output)
        self.assertIn("backend reported a load failure", logs)
        self.assertNotIn("insufficient_vram", logs)
        self.assertNotIn("private", logs)

    async def test_cold_cleanup_recovery_preserves_native_model_selection(self):
        self.tabby.model = "another-exl3"
        self.comfy.cleanup_failures = 1
        async with await self.chat() as response:
            self.assertEqual(response.status, 503)
        async with await self.chat() as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.tabby.model, "another-exl3")
        self.assertEqual(self.tabby.loads, [])
        self.assertEqual(self.tabby.unloads, 0)

    async def test_error_without_stream_end_does_not_allow_retry(self):
        self.backend._timeouts = BackendTimeouts(1, 1, 0.05, 0.01)
        self.tabby.model = "inactive"
        self.tabby.load_error = True
        self.tabby.hold_error_stream = True
        with self.assertRaises(UpstreamError):
            await self.backend.acquire_gpu(None)
        self.assertTrue(self.tabby.error_sent.is_set())
        with self.assertRaises(UpstreamError):
            await self.backend.acquire_gpu(None)
        with self.assertRaises(UpstreamError):
            await self.backend.observe_ready()
        self.assertEqual(len(self.tabby.loads), 1)

    async def test_delayed_load_with_wrong_saved_settings_stays_blocked(self):
        target = await self.backend.snapshot_ready()
        await self.backend.release_gpu(target)
        self.backend._timeouts = BackendTimeouts(1, 1, 0.05, 0.01)
        self.tabby.hold_load = True
        self.tabby.wrong_context = True
        with self.assertRaises(UpstreamError):
            await self.backend.acquire_gpu(target)
        self.tabby.load_release.set()
        await wait_until(lambda: self.tabby.model != "inactive")
        with self.assertRaises(UpstreamError):
            await self.backend.acquire_gpu(target)
        with self.assertRaises(UpstreamError):
            await self.backend.observe_ready()
        self.assertEqual(len(self.tabby.loads), 1)

    async def test_failed_unload_can_be_reconciled_when_confirmed_unloaded(self):
        target = await self.backend.snapshot_ready()
        self.tabby.unload_failures = 1
        with self.assertRaises(UpstreamError):
            await self.backend.release_gpu(target)
        with self.assertRaises(UpstreamError):
            await self.backend.observe_ready()
        self.tabby.model = "inactive"
        self.assertIsNone(await self.backend.observe_ready())
        self.assertEqual(await self.backend.acquire_gpu(target), target)

    async def test_wrong_context_is_not_ready(self):
        target = await self.backend.snapshot_ready()
        await self.backend.release_gpu(target)
        self.tabby.wrong_context = True
        with self.assertRaisesRegex(UpstreamError, "mismatch"):
            await self.backend.acquire_gpu(target)

    async def test_changed_profile_cache_is_rejected_on_restore(self):
        target = await self.backend.snapshot_ready()
        await self.backend.release_gpu(target)
        self.tabby.model_overrides[target.model] = {"cache_size": 16384}
        with self.assertRaisesRegex(UpstreamError, "mismatch"):
            await self.backend.acquire_gpu(target)

    async def test_cold_load_adopts_model_profile_and_restores_it_after_comfy(self):
        self.tabby.model = "inactive"
        self.tabby.model_overrides["synthetic-exl3"] = {
            "max_seq_len": 16384, "cache_size": 16384,
        }
        async with await self.chat() as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.tabby.loads[0]["max_seq_len"], 32768)
        self.assertEqual(self.tabby.parameters["max_seq_len"], 16384)
        (await self.image()).close()
        await self.comfy_ready()
        async with await self.chat() as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(len(self.tabby.loads), 2)
        self.assertEqual(self.tabby.loads[1]["max_seq_len"], 16384)
        self.assertEqual(self.tabby.loads[1]["cache_size"], 16384)
        self.assertEqual(self.tabby.events, ["load", "chat", "unload", "load", "chat"])

    async def test_passive_routes_do_not_take_gpu_from_comfy(self):
        (await self.image()).close()
        await self.comfy_ready()
        before = list(self.tabby.request_paths)
        unloads = self.tabby.unloads
        for path in ("/v1/models", "/v1/model/list", "/health"):
            async with self.client.get(self.chat_url + path) as response:
                self.assertEqual(response.status, 200)
                data = await response.json()
                if path != "/health":
                    self.assertEqual(
                        [model["id"] for model in data["data"]], self.tabby.available_models,
                    )
        self.assertEqual(
            self.tabby.request_paths, before + ["/v1/models", "/v1/model/list", "/health"],
        )
        for path in ("/v1/model", "/props"):
            async with self.client.get(self.chat_url + path) as response:
                self.assertEqual(response.status, 503)
        self.assertEqual(self.tabby.loads, [])
        self.assertEqual(self.tabby.unloads, unloads)
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

    async def test_adapted_payload_survives_handoff_with_total_limit_intact(self):
        payload = {
            "model": "native-name", "max_tokens": 101, "reasoning_effort": "medium",
            "messages": [{"role": "user", "content": "synthetic å"}],
            "temperature": 0.71, "unknown": 42,
        }
        for run in range(2):
            async with await self.chat(**payload) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())["choices"][0]["message"]["content"],
                                 "synthetic")
            self.assertEqual(json.loads(self.tabby.payloads[-1]), {
                **payload, "reasoning_budget_tokens": 50,
            })
            if not run:
                (await self.image()).close()
                await self.comfy_ready()
        self.assertEqual(self.tabby.events, ["chat", "unload", "load", "chat"])

    async def test_thinking_diagnostic_logs_only_safe_outgoing_values(self):
        private = "synthetic-private-marker"
        unrelated = {
            "messages": [{"role": "user", "content": private}],
            "model": private, "prompt": private, "response": private,
            "reasoning_content": private, "unknown": {"path": private},
        }
        cases = (
            ({"reasoning_effort": "low", "max_tokens": 1400},
             "low", "1400", "350"),
            ({}, "<absent>", "<absent>", "<absent>"),
            ({"reasoning_effort": None, "max_tokens": None, "reasoning_budget_tokens": None},
             "null", "null", "null"),
            ({"reasoning_effort": private + "\nforged log", "max_tokens": {"secret": private},
              "reasoning_budget_tokens": [private]}, "<other>", "<other>", "<other>"),
            ({"reasoning_effort": "medium", "max_tokens": True,
              "reasoning_budget_tokens": float("inf")}, "medium", "<other>", "<other>"),
            ({"reasoning_effort": "high", "max_tokens": 1400.0, "reasoning_budget_tokens": 12.5},
             "high", "1400.0", "12.5"),
        )
        for controls, effort, maximum, budget in cases:
            with self.subTest(controls=controls):
                with self.assertLogs("st_proxy.service", level="INFO") as logs:
                    async with self.client.post(
                        self.chat_url + "/v1/chat/completions", json={**unrelated, **controls},
                        headers={"Authorization": private, "X-Private": private},
                    ) as response:
                        self.assertEqual(response.status, 200)
                        await response.read()
                diagnostics = [record for record in logs.records
                               if record.getMessage().startswith("TabbyAPI request:")]
                self.assertEqual(len(diagnostics), 1)
                self.assertEqual(diagnostics[0].levelname, "INFO")
                self.assertEqual(
                    diagnostics[0].getMessage(),
                    f"TabbyAPI request: reasoning_effort={effort} max_tokens={maximum} "
                    f"reasoning_budget_tokens={budget}",
                )
                self.assertNotIn(private, "\n".join(logs.output))
                expected = {**unrelated, **controls}
                if budget == "350":
                    expected["reasoning_budget_tokens"] = 350
                self.assertEqual(json.loads(self.tabby.payloads[-1]), expected)

    async def test_chat_body_limit_applies_to_chunked_and_sized_requests(self):
        self.service.config = replace(self.config, max_chat_body_bytes=64)
        raw = json.dumps({"messages": ["x" * 100]}).encode()

        async def chunks():
            yield raw[:50]
            yield raw[50:]

        for data in (raw, chunks()):
            async with self.client.post(
                self.chat_url + "/v1/chat/completions", data=data,
            ) as response:
                self.assertEqual(response.status, 413)
        self.assertEqual(self.tabby.payloads, [])

    async def test_other_tabby_routes_bypass_request_adaptation(self):
        with patch("st_proxy.service.adapt_chat_request", side_effect=AssertionError):
            async with self.client.get(self.chat_url + "/v1/models") as response:
                self.assertEqual(response.status, 200)
            async with self.client.post(self.chat_url + "/v1/completions", json={}) as response:
                self.assertEqual(response.status, 404)

    async def test_disconnected_stream_drains_before_handoff(self):
        self.tabby.hold_chat = True
        response = await self.chat(stream=True, reasoning_effort="low", max_tokens=100)
        self.assertEqual(json.loads(self.tabby.payloads[-1])["reasoning_budget_tokens"], 25)
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

    async def test_reload_waits_five_seconds_after_comfy_cleanup(self):
        (await self.image()).close()
        await self.comfy_ready()
        loop = asyncio.get_running_loop()
        started = loop.time()
        chat = asyncio.create_task(self.chat())
        try:
            await wait_until(lambda: self.comfy.free_calls == 1)
            self.assertEqual(self.service.coordinator.status()["state"], "cleaning_comfy")
            self.assertEqual(self.tabby.loads, [])
            response = await chat
            response.close()
            self.assertEqual(response.status, 200)
            self.assertGreaterEqual(loop.time() - started, 5.0)
            self.assertEqual(len(self.tabby.loads), 1)
        finally:
            if not chat.done():
                chat.cancel()
                await asyncio.gather(chat, return_exceptions=True)

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

    async def test_existing_model_context_is_accepted_passively(self):
        self.tabby.parameters.update(max_seq_len=16384, cache_size=16384)
        observed = await self.backend.observe_ready()
        self.assertEqual(observed.parameters["max_seq_len"], 16384)
        self.assertEqual(observed.parameters["cache_size"], 16384)
        await check_backend(self.config, 1)
        self.assertEqual(self.tabby.loads, [])
        self.assertEqual(self.tabby.unloads, 0)

    async def test_invalid_observed_context_is_rejected(self):
        for context in (0, -1, None, True, "16384"):
            with self.subTest(context=context):
                self.tabby.parameters["max_seq_len"] = context
                with self.assertRaises(UpstreamError):
                    await self.backend.observe_ready()

    async def test_test_mode_rejects_tabby_production_port(self):
        with self.assertRaises(ConfigurationError):
            TestEndpointRegistry().approve_disposable("http://127.0.0.1:5003")
