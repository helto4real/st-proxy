from __future__ import annotations

import asyncio
import builtins
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientSession

from st_proxy.config import BrokerConfig, TestEndpointRegistry
from st_proxy.errors import ConfigurationError
from st_proxy.service import BrokerService

from .support import DynamicServer, MockComfy, MockKobold, prompt_payload, wait_until


class BrokerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="st-proxy-test-")
        self.old_cwd = os.getcwd()
        os.chdir(self.tempdir.name)
        self.kobold = MockKobold()
        self.comfy = MockComfy()
        self.kobold_server = DynamicServer(self.kobold.app())
        self.comfy_server = DynamicServer(self.comfy.app())
        kobold_url = await self.kobold_server.start()
        comfy_url = await self.comfy_server.start()
        self.registry = TestEndpointRegistry()
        self.registry.approve_disposable(kobold_url)
        self.registry.approve_disposable(comfy_url)
        self.config = BrokerConfig.for_test(
            kobold_url=kobold_url,
            comfy_url=comfy_url,
            registry=self.registry,
        )
        self.service = BrokerService(self.config)
        await self.service.start()
        self.client = ClientSession()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.service.stop()
        await self.comfy_server.close()
        await self.kobold_server.close()
        os.chdir(self.old_cwd)
        self.tempdir.cleanup()

    @property
    def chat_url(self) -> str:
        return f"http://127.0.0.1:{self.service.chat_port}"

    @property
    def image_url(self) -> str:
        return f"http://127.0.0.1:{self.service.image_port}"

    async def post_prompt(self):
        return await self.client.post(f"{self.image_url}/prompt", json=prompt_payload())

    async def status(self) -> dict:
        async with self.client.get(f"{self.chat_url}/broker/status") as response:
            self.assertEqual(response.status, 200)
            return await response.json()

    async def wait_ready(self) -> None:
        await wait_until(
            lambda: (
                self.service.coordinator is not None
                and self.service.coordinator.status()["state"] == "llm_ready"
                and not self.service.coordinator.status()["waiting_chats"]
                and not self.service.coordinator.status()["waiting_images"]
            )
        )

    async def wait_comfy_ready(self) -> None:
        await wait_until(
            lambda: (
                self.service.coordinator is not None
                and self.service.coordinator.status()["state"] == "comfy_ready"
                and not self.service.coordinator.status()["waiting_images"]
                and self.service.coordinator.status()["active_prompt_id"] is None
            )
        )

    async def test_complete_successful_handoff(self) -> None:
        async with await self.post_prompt() as response:
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["prompt_id"], "synthetic-1")
        await self.wait_comfy_ready()
        self.assertEqual(self.kobold.admin_calls, ["unload_model"])
        self.assertEqual(self.comfy.free_calls, 0)
        status = await self.status()
        self.assertEqual(status["state"], "comfy_ready")
        self.assertEqual(status["gpu_owner"], "comfy")
        self.assertTrue(status["chat_available"])

        async with self.client.post(f"{self.chat_url}/api/v1/generate", json={}) as chat:
            self.assertEqual(chat.status, 200)
        await self.wait_ready()
        self.assertEqual(self.kobold.admin_calls, ["unload_model", "initial_model"])
        self.assertEqual(self.comfy.free_calls, 1)
        self.assertEqual((await self.status())["state"], "llm_ready")

    async def test_kobold_metadata_queries_are_passive_while_comfy_owns_gpu(self) -> None:
        self.comfy.auto_complete = False
        response = await self.post_prompt()
        response.close()
        await wait_until(
            lambda: (
                self.service.coordinator is not None
                and self.service.coordinator.status()["state"] == "image_active"
            )
        )
        self.assertEqual(self.kobold.admin_calls, ["unload_model"])
        kobold_requests = tuple(self.kobold.request_paths)

        async with self.client.get(f"{self.chat_url}/api/v1/model") as metadata:
            self.assertEqual(metadata.status, 200)
            self.assertEqual((await metadata.json())["result"], "inactive")
        async with self.client.get(f"{self.chat_url}/v1/models") as metadata:
            self.assertEqual(metadata.status, 200)
            self.assertEqual(await metadata.json(), {"object": "list", "data": []})

        status = await self.status()
        self.assertEqual(status["state"], "image_active")
        self.assertEqual(status["gpu_owner"], "comfy")
        self.assertEqual(self.kobold.admin_calls, ["unload_model"])
        self.assertEqual(self.comfy.free_calls, 0)
        self.assertNotIn("/v1/models", self.kobold.metadata_paths)
        self.assertEqual(tuple(self.kobold.request_paths), kobold_requests)

        self.comfy.completion["synthetic-1"].set()
        await self.wait_comfy_ready()
        await asyncio.sleep(0.05)
        self.assertEqual(tuple(self.kobold.request_paths), kobold_requests)

    async def test_idle_timeout_never_restores_kobold_without_active_chat(self) -> None:
        await self.service.stop()
        free_calls_before_restart = self.comfy.free_calls
        self.config = BrokerConfig.for_test(
            kobold_url=self.kobold_server.url,
            comfy_url=self.comfy_server.url,
            registry=self.registry,
            idle_timeout=0.05,
            restore_llm_on_idle=True,
        )
        self.service = BrokerService(self.config)
        await self.service.start()

        response = await self.post_prompt()
        response.close()
        await self.wait_comfy_ready()
        await asyncio.sleep(0.12)
        status = await self.status()
        self.assertEqual(status["idle_timeout"], 0.05)
        self.assertFalse(status["idle_restore_enabled"])
        self.assertFalse(status["idle_restore_scheduled"])
        self.assertEqual(
            self.kobold.admin_calls,
            ["unload_model"],
        )
        self.assertEqual(self.comfy.free_calls, free_calls_before_restart)
        self.assertEqual(status["gpu_owner"], "comfy")

    async def test_startup_is_passive_until_active_work_arrives(self) -> None:
        self.assertEqual(self.comfy.free_calls, 0)
        self.assertEqual(self.kobold.version_paths, [])
        self.assertEqual(self.kobold.metadata_paths, [])
        self.assertEqual(self.kobold.admin_calls, [])
        self.assertEqual(self.kobold.request_paths, [])
        status = await self.status()
        self.assertEqual(status["state"], "awaiting_request")
        self.assertIsNone(status["gpu_owner"])
        self.assertTrue(status["chat_available"])
        self.assertTrue(status["healthy"])
        self.assertTrue(status["dispatcher_alive"])
        self.assertFalse(status["idle_restore_enabled"])
        self.assertEqual(status["active_chat_requests"], 0)
        self.assertEqual(status["active_image_requests"], 0)
        self.assertEqual(status["active_websockets"], 0)

    async def test_transport_classes_use_independent_connection_pools(self) -> None:
        sessions = {
            self.service.control_session,
            self.service.chat_session,
            self.service.image_session,
            self.service.websocket_session,
        }

        self.assertNotIn(None, sessions)
        self.assertEqual(len(sessions), 4)
        status = await self.status()
        self.assertEqual(status["control_connection_limit"], 16)
        self.assertEqual(status["chat_connection_limit"], 64)
        self.assertEqual(status["image_connection_limit"], 64)
        self.assertEqual(status["websocket_connection_limit"], 32)

    async def test_console_logs_requests_and_handoffs_without_sensitive_data(self) -> None:
        with self.assertLogs("st_proxy", level="INFO") as captured:
            async with self.client.post(
                f"{self.chat_url}/v1/chat/completions?token=query-secret",
                json={"prompt": "body-secret"},
            ) as chat:
                self.assertEqual(chat.status, 200)
            image = await self.post_prompt()
            image.close()
            await self.wait_comfy_ready()
            async with self.client.post(f"{self.chat_url}/api/v1/generate", json={}) as chat:
                self.assertEqual(chat.status, 200)
            await self.wait_ready()

        logs = "\n".join(captured.output)
        self.assertIn("target=KoboldCpp method=POST path=/v1/chat/completions", logs)
        self.assertIn("target=ComfyUI method=POST path=/prompt status=200", logs)
        self.assertIn("ComfyUI VRAM cleanup completed", logs)
        self.assertIn("GPU ownership transferred: owner=ComfyUI", logs)
        self.assertIn("GPU ownership transferred: owner=KoboldCpp", logs)
        self.assertNotIn("query-secret", logs)
        self.assertNotIn("body-secret", logs)
        self.assertNotIn("SyntheticNode", logs)

    async def test_routine_comfy_polling_is_debug_only(self) -> None:
        with self.assertLogs("st_proxy.service", level="DEBUG") as captured:
            async with self.client.get(f"{self.image_url}/history") as response:
                self.assertEqual(response.status, 200)
            async with self.client.get(
                f"{self.image_url}/helto_director/prompt_studio/bridge/jobs/status"
            ) as response:
                self.assertEqual(response.status, 200)
            async with self.client.get(f"{self.image_url}/api/jobs") as response:
                self.assertEqual(response.status, 200)

        history_logs = [message for message in captured.output if "path=/history" in message]
        self.assertEqual(len(history_logs), 2)
        self.assertTrue(all(message.startswith("DEBUG:") for message in history_logs))
        job_status_logs = [
            message for message in captured.output if "path=/helto_director/" in message
        ]
        self.assertEqual(len(job_status_logs), 2)
        self.assertTrue(all(message.startswith("DEBUG:") for message in job_status_logs))
        api_jobs_logs = [message for message in captured.output if "path=/api/jobs" in message]
        self.assertEqual(len(api_jobs_logs), 2)
        self.assertTrue(all(message.startswith("DEBUG:") for message in api_jobs_logs))

    async def test_comfy_websocket_stays_connected_across_gpu_handoffs(self) -> None:
        websocket = await self.client.ws_connect(
            f"{self.image_url}/ws?clientId=synthetic-browser",
            origin=self.image_url,
        )
        self.assertEqual(
            await websocket.receive_json(),
            {"type": "status", "client_id": "synthetic-browser"},
        )
        self.assertEqual(self.comfy.websocket_client_ids, ["synthetic-browser"])
        self.assertEqual(self.comfy.websocket_origins, [self.comfy_server.url])
        self.assertIsNone((await self.status())["gpu_owner"])
        self.assertEqual((await self.status())["active_websockets"], 1)

        image = await self.post_prompt()
        image.close()
        await self.wait_comfy_ready()
        await websocket.send_str("feature-flags")
        self.assertEqual((await websocket.receive()).data, "upstream:feature-flags")

        async with self.client.post(f"{self.chat_url}/api/v1/generate", json={}) as chat:
            self.assertEqual(chat.status, 200)
        await self.wait_ready()
        await websocket.send_bytes(b"still-connected")
        self.assertEqual((await websocket.receive()).data, b"upstream:still-connected")
        await websocket.close()
        await wait_until(
            lambda: self.service._active_websockets == 0  # noqa: SLF001
        )

    async def test_workflow_body_larger_than_limit_is_rejected_without_queueing(self) -> None:
        await self.service.stop()
        self.config = BrokerConfig.for_test(
            kobold_url=self.kobold_server.url,
            comfy_url=self.comfy_server.url,
            registry=self.registry,
            max_workflow_body_bytes=8,
            max_queued_workflow_bytes=8,
        )
        self.service = BrokerService(self.config)
        await self.service.start()

        async with self.client.post(
            f"{self.image_url}/prompt",
            data=b"123456789",
        ) as response:
            self.assertEqual(response.status, 413)

        async def chunked_body():
            yield b"1234"
            yield b"56789"

        async with self.client.post(
            f"{self.image_url}/prompt",
            data=chunked_body(),
        ) as response:
            self.assertEqual(response.status, 413)
        self.assertEqual(self.comfy.prompt_calls, [])

    async def test_comfy_gateway_rewrites_private_origins_and_redirects(self) -> None:
        async with self.client.get(
            f"{self.image_url}/inspect-origin",
            headers={
                "Origin": self.image_url,
                "Referer": f"{self.image_url}/workflows/local",
            },
        ) as response:
            self.assertEqual(
                await response.json(),
                {
                    "origin": self.comfy_server.url,
                    "referer": f"{self.comfy_server.url}/workflows/local",
                },
            )

        async with self.client.get(
            f"{self.image_url}/redirect-to-history",
            allow_redirects=False,
        ) as response:
            self.assertEqual(response.status, 302)
            self.assertEqual(
                response.headers["Location"],
                f"{self.image_url}/history?from=upstream",
            )

    async def test_api_prompt_alias_uses_the_gpu_coordinator(self) -> None:
        async with self.client.post(
            f"{self.image_url}/api/prompt",
            json=prompt_payload(),
        ) as response:
            self.assertEqual(response.status, 200)
        await self.wait_comfy_ready()
        self.assertEqual(self.kobold.admin_calls, ["unload_model"])

    async def test_userdata_api_alias_uses_the_legacy_upstream_route(self) -> None:
        async with self.client.get(
            f"{self.image_url}/api/userdata/workflows%2Fsynthetic.json",
        ) as response:
            self.assertEqual(response.status, 200)

        self.assertEqual(self.comfy.userdata_requests, ["workflows/synthetic.json"])
        self.assertNotIn(("GET", "/userdata/workflows/synthetic.json"), self.comfy.generic_requests)

    async def test_external_comfy_lifecycle_request_is_rejected(self) -> None:
        free_calls = self.comfy.free_calls

        for path in ("/free", "/api/free"):
            async with self.client.post(
                f"{self.image_url}{path}",
                json={"unload_models": True, "free_memory": True},
            ) as response:
                self.assertEqual(response.status, 403)

        self.assertEqual(self.comfy.free_calls, free_calls)

    async def test_unknown_comfy_mutation_passes_through_by_default(self) -> None:
        async with self.client.post(f"{self.image_url}/custom-node/run-model") as response:
            self.assertEqual(response.status, 200)

        self.assertIn(
            ("POST", "/custom-node/run-model"),
            self.comfy.generic_requests,
        )
        self.assertEqual((await self.status())["comfy_route_policy"], "transparent")
        self.assertEqual(self.kobold.admin_calls, [])

    async def test_helto_privacy_decrypt_passes_without_gpu_handoff(self) -> None:
        async with self.client.post(
            f"{self.image_url}/helto_director/privacy/decrypt",
            json={"payload": {"synthetic": True}},
        ) as response:
            self.assertEqual(response.status, 200)

        self.assertIn(
            ("POST", "/helto_director/privacy/decrypt"),
            self.comfy.generic_requests,
        )
        self.assertEqual(self.kobold.admin_calls, [])

    async def test_h3_preview_decrypt_api_route_passes_without_gpu_handoff(self) -> None:
        async with self.client.post(
            f"{self.image_url}/api/helto_director/h3_preview/decrypt",
            json={"envelope": {"synthetic": True}},
        ) as response:
            self.assertEqual(response.status, 200)

        self.assertIn(
            ("POST", "/api/helto_director/h3_preview/decrypt"),
            self.comfy.generic_requests,
        )
        self.assertEqual(self.kobold.admin_calls, [])

    async def test_reviewed_extension_mutations_pass_without_gpu_handoff(self) -> None:
        paths = (
            "/api/helto_queue_manager/state",
            "/helto_selector/delete_images",
            "/helto_director/library/projects/synthetic-id/preview",
            "/helto_spm/privacy/decrypt",
            "/aio_image_generate/privacy/decrypt",
        )

        for path in paths:
            async with self.client.post(
                f"{self.image_url}{path}",
                json={"synthetic": True},
            ) as response:
                self.assertEqual(response.status, 200)

        for path in paths:
            self.assertIn(("POST", path), self.comfy.generic_requests)
        self.assertEqual(self.kobold.admin_calls, [])

    async def test_gpu_capable_optimizer_mutation_remains_blocked(self) -> None:
        path = "/helto_director/prompt_optimizer/optimize/start"
        async with self.client.post(
            f"{self.image_url}{path}",
            json={"synthetic": True},
        ) as response:
            self.assertEqual(response.status, 403)

        self.assertNotIn(("POST", path), self.comfy.generic_requests)

    async def test_unknown_comfy_mutation_can_be_rejected_in_strict_mode(self) -> None:
        await self.service.stop()
        self.config = BrokerConfig.for_test(
            kobold_url=self.kobold_server.url,
            comfy_url=self.comfy_server.url,
            registry=self.registry,
            allow_unknown_comfy_routes=False,
        )
        self.service = BrokerService(self.config)
        await self.service.start()

        async with self.client.post(f"{self.image_url}/custom-node/settings") as response:
            self.assertEqual(response.status, 403)

        self.assertNotIn(("POST", "/custom-node/settings"), self.comfy.generic_requests)
        self.assertEqual((await self.status())["comfy_route_policy"], "strict")

    async def test_api_interrupt_alias_is_coordinated_with_active_comfy_workflow(self) -> None:
        self.comfy.auto_complete = False
        image = await self.post_prompt()
        image.close()
        await wait_until(lambda: self.comfy.prompt_calls == ["synthetic-1"])

        async with self.client.post(f"{self.image_url}/api/interrupt") as response:
            self.assertEqual(response.status, 200)

        await self.wait_comfy_ready()
        self.assertEqual(self.comfy.interrupt_calls, 1)
        self.assertEqual(self.kobold.admin_calls, ["unload_model"])

    async def test_interrupt_is_rejected_when_comfy_does_not_own_gpu(self) -> None:
        async with self.client.post(f"{self.image_url}/interrupt") as response:
            self.assertEqual(response.status, 409)

        self.assertEqual(self.comfy.interrupt_calls, 0)

    async def test_custom_control_is_allowed_only_while_comfy_owns_gpu(self) -> None:
        route = "/api/helto_save_image_advanced/release"
        async with self.client.post(f"{self.image_url}{route}") as response:
            self.assertEqual(response.status, 409)
        self.assertNotIn(("POST", route), self.comfy.generic_requests)

        self.comfy.auto_complete = False
        image = await self.post_prompt()
        image.close()
        await wait_until(lambda: self.comfy.prompt_calls == ["synthetic-1"])

        async with self.client.post(f"{self.image_url}{route}") as response:
            self.assertEqual(response.status, 200)
        self.assertIn(("POST", route), self.comfy.generic_requests)

        async with self.client.post(f"{self.image_url}/interrupt") as response:
            self.assertEqual(response.status, 200)
        await self.wait_comfy_ready()
        self.assertEqual(self.kobold.admin_calls, ["unload_model"])

    async def test_active_chat_fails_before_kobold_when_comfy_cannot_be_freed(self) -> None:
        await self.service.stop()
        self.comfy.cleanup_failures = 1
        self.service = BrokerService(self.config)
        await self.service.start()

        self.assertEqual((await self.status())["state"], "awaiting_request")
        async with self.client.post(f"{self.chat_url}/api/v1/generate", json={}) as chat:
            self.assertEqual(chat.status, 503)
        status = await self.status()
        self.assertEqual(status["state"], "error")
        self.assertFalse(status["ready"])
        self.assertIn("model cleanup", status["last_error"])
        self.assertEqual(self.kobold.chat_requests, 0)
        self.assertEqual(self.kobold.version_paths, [])

    async def test_active_work_fails_closed_when_kobold_admin_is_disabled(self) -> None:
        await self.service.stop()
        free_calls_before_restart = self.comfy.free_calls
        self.kobold.admin_enabled = False
        self.service = BrokerService(self.config)
        with self.assertLogs("st_proxy", level="INFO") as captured:
            await self.service.start()
            response = await self.post_prompt()
            self.assertEqual(response.status, 503)
            response.close()

        status = await self.status()
        self.assertEqual(status["state"], "error")
        self.assertFalse(status["ready"])
        self.assertIn("model administration check failed", status["last_error"])
        self.assertEqual(self.comfy.free_calls, free_calls_before_restart)
        logs = "\n".join(captured.output)
        self.assertIn("enable Model Administration", logs)

    async def test_first_active_chat_loads_inactive_kobold_once(self) -> None:
        await self.service.stop()
        self.kobold.model = "inactive"
        self.config = BrokerConfig.for_test(
            kobold_url=self.kobold_server.url,
            comfy_url=self.comfy_server.url,
            registry=self.registry,
            reload_timeout=0.05,
        )
        self.service = BrokerService(self.config)
        await self.service.start()

        self.assertEqual((await self.status())["state"], "awaiting_request")
        async with self.client.post(f"{self.chat_url}/api/v1/generate", json={}) as chat:
            self.assertEqual(chat.status, 200)

        status = await self.status()
        self.assertEqual(status["state"], "llm_ready")
        self.assertEqual(status["gpu_owner"], "llm")
        self.assertEqual(self.comfy.free_calls, 1)
        self.assertEqual(self.kobold.admin_calls, ["initial_model"])
        self.assertEqual(self.kobold.chat_requests, 1)

    async def test_streaming_chat_finishes_before_unload(self) -> None:
        self.kobold.hold_chat = True
        chat_response = await self.client.post(f"{self.chat_url}/api/extra/generate/stream")
        first = await chat_response.content.readuntil(b"\n\n")
        self.assertEqual(first, b"data: synthetic-one\n\n")
        image_task = asyncio.create_task(self.post_prompt())
        await asyncio.sleep(0.03)
        self.assertEqual(self.kobold.admin_calls, [])
        self.kobold.chat_release.set()
        rest = await chat_response.read()
        self.assertIn(b"synthetic-two", rest)
        chat_response.close()
        image_response = await image_task
        image_response.close()
        await self.wait_comfy_ready()
        self.assertLess(
            self.kobold.events.index("chat_finished"), self.kobold.events.index("unload_model")
        )

    async def test_chat_waits_while_image_owns_gpu(self) -> None:
        self.comfy.auto_complete = False
        image_response = await self.post_prompt()
        image_response.close()
        await wait_until(lambda: self.comfy.prompt_calls == ["synthetic-1"])
        chat_task = asyncio.create_task(
            self.client.post(f"{self.chat_url}/api/v1/generate", json={"prompt": "synthetic"})
        )
        await asyncio.sleep(0.03)
        self.assertEqual(self.kobold.chat_requests, 0)
        self.assertEqual(self.kobold.admin_calls, ["unload_model"])
        self.comfy.completion["synthetic-1"].set()
        chat_response = await chat_task
        self.assertEqual(chat_response.status, 200)
        chat_response.close()
        await self.wait_ready()
        self.assertEqual(
            self.kobold.admin_calls,
            ["unload_model", "initial_model"],
        )
        self.assertEqual(self.comfy.free_calls, 1)

    async def test_chat_drain_timeout_does_not_restart_active_kobold(self) -> None:
        await self.service.stop()
        self.kobold.hold_chat = True
        self.config = BrokerConfig.for_test(
            kobold_url=self.kobold_server.url,
            comfy_url=self.comfy_server.url,
            registry=self.registry,
            chat_drain_timeout=0.05,
        )
        self.service = BrokerService(self.config)
        await self.service.start()
        chat_response = await self.client.post(f"{self.chat_url}/api/extra/generate/stream")
        await chat_response.content.readuntil(b"\n\n")
        image_response = await self.post_prompt()
        self.assertEqual(image_response.status, 503)
        image_response.close()
        self.assertEqual(self.kobold.admin_calls, [])
        self.assertEqual(self.comfy.prompt_calls, [])
        self.assertEqual((await self.status())["state"], "llm_ready")
        self.kobold.chat_release.set()
        await chat_response.read()
        chat_response.close()

    async def test_stalled_chat_stream_releases_lease_after_read_timeout(self) -> None:
        await self.service.stop()
        self.kobold.hold_chat = True
        self.config = BrokerConfig.for_test(
            kobold_url=self.kobold_server.url,
            comfy_url=self.comfy_server.url,
            registry=self.registry,
            connect_timeout=0.05,
            request_timeout=0.5,
        )
        self.service = BrokerService(self.config)
        await self.service.start()

        chat_response = await self.client.post(f"{self.chat_url}/api/extra/generate/stream")
        self.assertEqual(
            await chat_response.content.readuntil(b"\n\n"),
            b"data: synthetic-one\n\n",
        )
        await wait_until(
            lambda: (
                self.service.coordinator is not None
                and self.service.coordinator.status()["active_chats"] == 0
            )
        )
        chat_response.close()
        self.kobold.chat_release.set()
        await asyncio.sleep(0.05)

        image = await self.post_prompt()
        self.assertEqual(image.status, 200)
        image.close()
        await self.wait_comfy_ready()

    async def test_concurrent_image_requests_are_fifo_batched_before_chat(self) -> None:
        self.comfy.auto_complete = False
        first = await self.post_prompt()
        first.close()
        second_task = asyncio.create_task(self.post_prompt())
        await wait_until(
            lambda: (
                self.service.coordinator is not None
                and self.service.coordinator.status()["waiting_images"] == 1
            )
        )
        chat_task = asyncio.create_task(
            self.client.post(f"{self.chat_url}/api/v1/generate", json={"prompt": "synthetic"})
        )
        await wait_until(
            lambda: (
                self.service.coordinator is not None
                and self.service.coordinator.status()["waiting_chats"] == 1
            )
        )
        self.assertEqual(self.comfy.prompt_calls, ["synthetic-1"])
        self.comfy.completion["synthetic-1"].set()
        second = await second_task
        self.assertEqual((await second.json())["prompt_id"], "synthetic-2")
        second.close()
        self.assertEqual(self.kobold.admin_calls, ["unload_model"])
        self.assertEqual(self.kobold.chat_requests, 0)
        self.comfy.completion["synthetic-2"].set()
        chat = await chat_task
        self.assertEqual(chat.status, 200)
        chat.close()
        await self.wait_ready()
        self.assertEqual(
            self.kobold.admin_calls,
            ["unload_model", "initial_model"],
        )
        self.assertEqual(self.comfy.free_calls, 1)

    async def test_http_workflow_queue_is_bounded_and_cleans_up_cancelled_request(self) -> None:
        await self.service.stop()
        self.comfy.auto_complete = False
        self.config = BrokerConfig.for_test(
            kobold_url=self.kobold_server.url,
            comfy_url=self.comfy_server.url,
            registry=self.registry,
            max_queued_images=1,
        )
        self.service = BrokerService(self.config)
        await self.service.start()

        first = await self.post_prompt()
        first.close()
        queued = asyncio.create_task(self.post_prompt())
        await wait_until(
            lambda: (
                self.service.coordinator is not None
                and self.service.coordinator.status()["waiting_images"] == 1
            )
        )

        rejected = await self.post_prompt()
        self.assertEqual(rejected.status, 429)
        self.assertEqual(rejected.headers["Retry-After"], "1")
        rejected.close()

        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
        await wait_until(
            lambda: (
                self.service.coordinator is not None
                and self.service.coordinator.status()["waiting_images"] == 0
                and self.service.coordinator.status()["queued_workflow_bytes"] == 0
            )
        )
        self.comfy.completion["synthetic-1"].set()
        await self.wait_comfy_ready()
        self.assertEqual(self.comfy.prompt_calls, ["synthetic-1"])

    async def test_fifo_does_not_batch_images_across_a_queued_chat(self) -> None:
        self.comfy.auto_complete = False
        first = await self.post_prompt()
        first.close()
        chat_task = asyncio.create_task(
            self.client.post(f"{self.chat_url}/api/extra/generate/stream")
        )
        await wait_until(
            lambda: (
                self.service.coordinator is not None
                and self.service.coordinator.status()["waiting_chats"] == 1
            )
        )
        second_task = asyncio.create_task(self.post_prompt())
        await wait_until(
            lambda: (
                self.service.coordinator is not None
                and self.service.coordinator.status()["waiting_images"] == 1
            )
        )

        self.kobold.hold_chat = True
        self.comfy.completion["synthetic-1"].set()
        chat = await chat_task
        await self.kobold.chat_started.wait()
        self.assertEqual(await chat.content.readuntil(b"\n\n"), b"data: synthetic-one\n\n")
        self.assertEqual(self.comfy.prompt_calls, ["synthetic-1"])
        self.assertEqual(self.kobold.admin_calls, ["unload_model", "initial_model"])

        self.kobold.chat_release.set()
        await chat.read()
        chat.close()
        second = await second_task
        self.assertEqual((await second.json())["prompt_id"], "synthetic-2")
        second.close()
        self.assertEqual(
            self.kobold.admin_calls,
            ["unload_model", "initial_model", "unload_model"],
        )
        self.comfy.completion["synthetic-2"].set()
        await self.wait_comfy_ready()

    async def test_image_generation_failure_does_not_restore_kobold(self) -> None:
        self.comfy.fail_next_job = True
        response = await self.post_prompt()
        self.assertEqual(response.status, 200)
        response.close()
        kobold_requests = tuple(self.kobold.request_paths)
        await self.wait_comfy_ready()
        status = await self.status()
        self.assertIn("image job failed", status["last_error"])
        self.assertEqual(self.kobold.model, "inactive")
        self.assertEqual(self.kobold.admin_calls, ["unload_model"])
        self.assertEqual(tuple(self.kobold.request_paths), kobold_requests)

    async def test_image_timeout_interrupts_without_restoring_kobold(self) -> None:
        await self.service.stop()
        self.comfy.auto_complete = False
        self.config = BrokerConfig.for_test(
            kobold_url=self.kobold_server.url,
            comfy_url=self.comfy_server.url,
            registry=self.registry,
            image_timeout=0.05,
        )
        self.service = BrokerService(self.config)
        await self.service.start()
        response = await self.post_prompt()
        response.close()
        kobold_requests = tuple(self.kobold.request_paths)
        await self.wait_comfy_ready()
        self.assertEqual(self.comfy.interrupt_calls, 1)
        self.assertEqual(self.kobold.model, "inactive")
        self.assertEqual(self.kobold.admin_calls, ["unload_model"])
        self.assertIn("timed out", (await self.status())["last_error"])
        self.assertEqual(tuple(self.kobold.request_paths), kobold_requests)

    async def test_repeated_history_failures_abort_without_waiting_for_image_timeout(self) -> None:
        await self.service.stop()
        self.comfy.auto_complete = False
        self.comfy.history_failures = 10
        self.config = BrokerConfig.for_test(
            kobold_url=self.kobold_server.url,
            comfy_url=self.comfy_server.url,
            registry=self.registry,
            image_timeout=5,
            comfy_poll_failure_limit=2,
        )
        self.service = BrokerService(self.config)
        await self.service.start()

        response = await self.post_prompt()
        self.assertEqual(response.status, 200)
        response.close()
        kobold_requests = tuple(self.kobold.request_paths)
        await self.wait_comfy_ready()

        status = await self.status()
        self.assertIn("history monitoring", status["last_error"])
        self.assertEqual(self.comfy.interrupt_calls, 1)
        self.assertEqual(self.kobold.admin_calls, ["unload_model"])
        self.assertEqual(tuple(self.kobold.request_paths), kobold_requests)

    async def test_unload_failure_does_not_restore_and_releases_lock(self) -> None:
        self.kobold.unload_failures = 1
        response = await self.post_prompt()
        self.assertEqual(response.status, 503)
        response.close()
        await wait_until(
            lambda: (
                self.service.coordinator is not None
                and self.service.coordinator.status()["state"] == "error"
            )
        )
        self.assertEqual(self.kobold.admin_calls, ["unload_model"])
        second = await self.post_prompt()
        self.assertEqual(second.status, 200)
        second.close()
        await self.wait_comfy_ready()

    async def test_admin_rejection_is_not_treated_as_a_successful_unload(self) -> None:
        async with self.client.post(f"{self.chat_url}/api/v1/generate", json={}) as chat:
            self.assertEqual(chat.status, 200)
        self.kobold.admin_enabled = False
        with self.assertLogs("st_proxy", level="INFO") as captured:
            response = await self.post_prompt()
            self.assertEqual(response.status, 503)
            response.close()

        status = await self.status()
        self.assertEqual(status["state"], "error")
        self.assertFalse(status["ready"])
        self.assertIn("admin request rejected", status["last_error"])
        logs = "\n".join(captured.output)
        self.assertNotIn(
            "KoboldCpp admin operation completed: config=unload_model",
            logs,
        )

    async def test_reload_failure_fails_closed(self) -> None:
        self.kobold.reload_failures = 1
        response = await self.post_prompt()
        self.assertEqual(response.status, 200)
        response.close()
        await self.wait_comfy_ready()
        async with self.client.post(f"{self.chat_url}/api/v1/generate", json={}) as chat:
            self.assertEqual(chat.status, 503)
        await wait_until(
            lambda: (
                self.service.coordinator is not None
                and self.service.coordinator.status()["state"] == "error"
            )
        )
        self.assertIn("initial_model", self.kobold.admin_calls)

    async def test_reload_requires_the_same_model_seen_at_startup(self) -> None:
        await self.service.stop()
        self.config = BrokerConfig.for_test(
            kobold_url=self.kobold_server.url,
            comfy_url=self.comfy_server.url,
            registry=self.registry,
            reload_timeout=0.05,
        )
        self.service = BrokerService(self.config)
        await self.service.start()
        self.kobold.reload_model = "unexpected-model.gguf"

        response = await self.post_prompt()
        response.close()
        await self.wait_comfy_ready()
        async with self.client.post(f"{self.chat_url}/api/v1/generate", json={}) as chat:
            self.assertEqual(chat.status, 503)

        status = await self.status()
        self.assertEqual(status["state"], "error")
        self.assertIn("confirmation of loaded", status["last_error"])

    async def test_comfy_cleanup_failure_recovers_only_for_next_active_chat(self) -> None:
        await self.service.stop()
        self.config = BrokerConfig.for_test(
            kobold_url=self.kobold_server.url,
            comfy_url=self.comfy_server.url,
            registry=self.registry,
        )
        self.service = BrokerService(self.config)
        await self.service.start()
        chat_port = self.service.chat_port
        image_port = self.service.image_port
        self.comfy.cleanup_failures = 1
        self.comfy.cleanup_failure_status = 409
        response = await self.post_prompt()
        response.close()
        await self.wait_comfy_ready()
        async with self.client.post(f"{self.chat_url}/api/v1/generate", json={}) as chat:
            self.assertEqual(chat.status, 503)
        await wait_until(
            lambda: (
                self.service.coordinator is not None
                and self.service.coordinator.status()["state"] == "error"
            )
        )
        self.assertEqual(self.kobold.model, "inactive")
        self.assertNotIn("initial_model", self.kobold.admin_calls)
        degraded = await self.status()
        self.assertTrue(degraded["responding"])
        self.assertFalse(degraded["ready"])
        self.assertFalse(degraded["recovering"])
        self.assertIn("model cleanup", degraded["last_error"])

        kobold_requests = tuple(self.kobold.request_paths)
        await asyncio.sleep(0.15)
        self.assertNotIn("initial_model", self.kobold.admin_calls)
        self.assertEqual(tuple(self.kobold.request_paths), kobold_requests)
        async with self.client.post(f"{self.chat_url}/api/v1/generate", json={}) as chat:
            self.assertEqual(chat.status, 200)
        await self.wait_ready()
        recovered = await self.status()
        self.assertEqual(self.service.chat_port, chat_port)
        self.assertEqual(self.service.image_port, image_port)
        self.assertTrue(recovered["healthy"])
        self.assertTrue(recovered["ready"])
        self.assertFalse(recovered["recovering"])
        self.assertEqual(recovered["recovery_attempts"], 2)
        self.assertIsNone(recovered["last_error"])
        self.assertIn("initial_model", self.kobold.admin_calls)

    async def test_disconnected_stream_is_drained_before_handoff(self) -> None:
        self.kobold.hold_chat = True
        chat_response = await self.client.post(f"{self.chat_url}/api/extra/generate/stream")
        await chat_response.content.readuntil(b"\n\n")
        chat_response.close()
        image_task = asyncio.create_task(self.post_prompt())
        await asyncio.sleep(0.03)
        self.assertEqual(self.kobold.admin_calls, [])
        self.kobold.chat_release.set()
        image_response = await image_task
        image_response.close()
        await self.wait_comfy_ready()

    async def test_shutdown_interrupts_active_handoff_without_restore(self) -> None:
        self.comfy.auto_complete = False
        response = await self.post_prompt()
        response.close()
        await wait_until(lambda: self.comfy.prompt_calls == ["synthetic-1"])
        kobold_requests = tuple(self.kobold.request_paths)
        await self.service.stop()
        await wait_until(lambda: self.comfy.interrupt_calls == 1)
        self.assertEqual(self.comfy.free_calls, 0)
        self.assertEqual(self.kobold.admin_calls, ["unload_model"])
        self.assertEqual(tuple(self.kobold.request_paths), kobold_requests)

    async def test_admin_secret_is_used_but_not_exposed_in_status(self) -> None:
        await self.service.stop()
        self.config = BrokerConfig.for_test(
            kobold_url=self.kobold_server.url,
            comfy_url=self.comfy_server.url,
            registry=self.registry,
            kobold_admin_password="synthetic-secret",
        )
        self.service = BrokerService(self.config)
        await self.service.start()
        response = await self.post_prompt()
        response.close()
        await self.wait_comfy_ready()
        async with self.client.post(f"{self.chat_url}/api/v1/generate", json={}) as chat:
            self.assertEqual(chat.status, 200)
        await self.wait_ready()
        self.assertEqual(self.kobold.admin_authorization, ["Bearer synthetic-secret"] * 2)
        self.assertNotIn("synthetic-secret", str(await self.status()))

    async def test_test_mode_only_accepts_registered_dynamic_endpoints(self) -> None:
        used_ports = {
            int(self.kobold_server.url.rsplit(":", 1)[1]),
            int(self.comfy_server.url.rsplit(":", 1)[1]),
            self.service.chat_port,
            self.service.image_port,
        }
        self.assertTrue(used_ports.isdisjoint({5001, 5002, 8188, 8189}))
        with self.assertRaises(ConfigurationError):
            BrokerConfig(
                chat_port=0,
                image_port=0,
                kobold_url="http://127.0.0.1:5001",
                comfy_url=self.comfy_server.url,
                test_mode=True,
                test_registry=self.registry,
            )
        other_registry = TestEndpointRegistry()
        with self.assertRaises(ConfigurationError):
            other_registry.approve_disposable("https://example.com:443")
        with self.assertRaises(ConfigurationError):
            other_registry.approve_disposable("http://127.0.0.1:8188")

    async def test_broker_does_not_write_outside_disposable_directory(self) -> None:
        allowed = Path(self.tempdir.name).resolve()
        original_open = builtins.open

        def guarded_open(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                target = Path(file).resolve()
                if not target.is_relative_to(allowed):
                    raise AssertionError(f"write escaped disposable directory: {target}")
            return original_open(file, mode, *args, **kwargs)

        with patch("builtins.open", guarded_open):
            response = await self.post_prompt()
            response.close()
            await self.wait_comfy_ready()
        self.assertEqual(list(allowed.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
