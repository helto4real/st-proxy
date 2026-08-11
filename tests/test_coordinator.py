from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass, field

from st_proxy.comfy import PromptResult
from st_proxy.config import BrokerConfig
from st_proxy.coordinator import HandoffCoordinator
from st_proxy.errors import ChatUnavailable, HandoffError, QueueFull, UpstreamError
from st_proxy.http import BufferedResponse
from st_proxy.llm import BackendInfo

from .support import wait_until


@dataclass(frozen=True, slots=True)
class FakeRestorePoint:
    identity: str
    backend_kind: str = "fake"


@dataclass
class FakeLlmBackend:
    calls: list[str] = field(default_factory=list)
    release_failures: int = 0
    acquire_failures: int = 0
    info: BackendInfo = field(
        default_factory=lambda: BackendInfo(
            "fake",
            "Fake LLM",
            "http://127.0.0.1:1",
        )
    )

    async def validate_control(self) -> None:
        self.calls.append("validate")

    async def snapshot_ready(self) -> FakeRestorePoint:
        self.calls.append("snapshot")
        return FakeRestorePoint("startup-model")

    async def release_gpu(self, target: FakeRestorePoint) -> None:
        assert target.identity == "startup-model"
        self.calls.append("release")
        if self.release_failures:
            self.release_failures -= 1
            raise UpstreamError("synthetic release failure")

    async def acquire_gpu(self, target: FakeRestorePoint) -> None:
        assert target.identity == "startup-model"
        self.calls.append("acquire")
        if self.acquire_failures:
            self.acquire_failures -= 1
            raise UpstreamError("synthetic acquire failure")


@dataclass
class FakeComfyClient:
    calls: list[str] = field(default_factory=list)
    completion: asyncio.Event = field(default_factory=asyncio.Event)
    free_failures: int = 0

    async def free_models(self) -> None:
        self.calls.append("free")
        if self.free_failures:
            self.free_failures -= 1
            raise UpstreamError("synthetic cleanup failure")

    async def submit(
        self,
        *,
        path: str,
        query_string: str,
        headers: dict[str, str],
        body: bytes,
    ) -> PromptResult:
        assert path == "/prompt"
        assert not query_string
        assert isinstance(headers, dict)
        assert body == b"{}"
        self.calls.append("submit")
        return PromptResult(
            BufferedResponse(200, "OK", (), b'{"prompt_id":"fake-prompt"}'),
            "fake-prompt",
        )

    async def wait_for_prompt(self, prompt_id: str) -> None:
        assert prompt_id == "fake-prompt"
        self.calls.append("wait")
        await self.completion.wait()

    async def interrupt(self) -> None:
        self.calls.append("interrupt")


class CoordinatorContractTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.llm = FakeLlmBackend()
        self.comfy = FakeComfyClient()
        self.coordinator = HandoffCoordinator(
            BrokerConfig(),
            self.llm,
            self.comfy,  # type: ignore[arg-type]
        )
        self.assertTrue(await self.coordinator.initialize())

    async def asyncTearDown(self) -> None:
        await self.coordinator.close()

    async def submit_image(self) -> BufferedResponse:
        return await self.coordinator.submit_image(
            path="/prompt",
            query_string="",
            headers={},
            body=b"{}",
        )

    async def restart_with_idle_timeout(self, idle_timeout: float) -> None:
        await self.restart_with_config(
            idle_timeout=idle_timeout,
            restore_llm_on_idle=True,
        )

    async def restart_with_config(self, **overrides: object) -> None:
        await self.coordinator.close()
        self.llm = FakeLlmBackend()
        self.comfy = FakeComfyClient()
        self.coordinator = HandoffCoordinator(
            BrokerConfig(**overrides),
            self.llm,
            self.comfy,  # type: ignore[arg-type]
        )
        self.assertTrue(await self.coordinator.initialize())

    async def test_coordinator_uses_only_generic_llm_lifecycle_contract(self) -> None:
        response = await self.submit_image()
        self.assertEqual(response.status, 200)
        self.comfy.completion.set()
        await wait_until(lambda: self.coordinator.status()["state"] == "comfy_ready")

        async with self.coordinator.chat_lease():
            pass

        self.assertEqual(
            self.llm.calls,
            ["validate", "snapshot", "release", "acquire"],
        )
        self.assertEqual(self.comfy.calls, ["free", "submit", "wait", "free"])
        self.assertEqual(self.coordinator.status()["llm_backend"], "fake")
        self.assertTrue(self.coordinator.status()["healthy"])
        self.assertTrue(self.coordinator.status()["dispatcher_alive"])

    async def test_cancelled_queued_image_releases_body_and_queue_capacity(self) -> None:
        first = await self.submit_image()
        self.assertEqual(first.status, 200)
        await wait_until(lambda: self.coordinator.status()["state"] == "image_active")

        abandoned = asyncio.create_task(self.submit_image())
        await wait_until(lambda: self.coordinator.status()["waiting_images"] == 1)
        self.assertEqual(self.coordinator.status()["queued_workflow_bytes"], 2)

        abandoned.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await abandoned
        await wait_until(lambda: self.coordinator.status()["waiting_images"] == 0)
        self.assertEqual(self.coordinator.status()["queued_workflow_bytes"], 0)

        self.comfy.completion.set()
        await wait_until(lambda: self.coordinator.status()["state"] == "comfy_ready")
        self.assertEqual(self.comfy.calls.count("submit"), 1)

    async def test_workflow_queue_rejects_requests_beyond_bounded_capacity(self) -> None:
        await self.restart_with_config(max_queued_images=1)
        first = await self.submit_image()
        self.assertEqual(first.status, 200)
        queued = asyncio.create_task(self.submit_image())
        await wait_until(lambda: self.coordinator.status()["waiting_images"] == 1)

        with self.assertRaisesRegex(QueueFull, "request limit"):
            await self.submit_image()

        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued
        self.comfy.completion.set()
        await wait_until(lambda: self.coordinator.status()["state"] == "comfy_ready")

    async def test_workflow_body_limit_is_enforced_inside_coordinator(self) -> None:
        await self.restart_with_config(
            max_workflow_body_bytes=1,
            max_queued_workflow_bytes=1,
        )

        with self.assertRaisesRegex(HandoffError, "size limit"):
            await self.submit_image()

    async def test_active_chat_is_drained_before_generic_release(self) -> None:
        async with self.coordinator.chat_lease():
            image_task = asyncio.create_task(self.submit_image())
            await wait_until(
                lambda: self.coordinator.status()["state"] == "draining_llm"
            )
            self.assertNotIn("release", self.llm.calls)

        response = await image_task
        self.assertEqual(response.status, 200)
        self.assertIn("release", self.llm.calls)
        self.comfy.completion.set()
        await wait_until(lambda: self.coordinator.status()["state"] == "comfy_ready")

    async def test_cancelled_image_stops_a_pending_gpu_handoff(self) -> None:
        async with self.coordinator.chat_lease():
            image_task = asyncio.create_task(self.submit_image())
            await wait_until(
                lambda: self.coordinator.status()["state"] == "draining_llm"
            )
            image_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await image_task
            await wait_until(
                lambda: self.coordinator.status()["state"] == "llm_ready"
            )

        self.assertNotIn("release", self.llm.calls)
        self.assertEqual(self.comfy.calls, ["free"])

    async def test_release_failure_restores_generic_backend(self) -> None:
        self.llm.release_failures = 1

        with self.assertRaisesRegex(HandoffError, "synthetic release failure"):
            await self.submit_image()

        await wait_until(lambda: self.coordinator.status()["state"] == "llm_ready")
        self.assertEqual(
            self.llm.calls,
            ["validate", "snapshot", "release", "acquire"],
        )

    async def test_idle_timeout_restores_generic_backend(self) -> None:
        await self.restart_with_idle_timeout(0.02)

        with self.assertLogs("st_proxy.coordinator", level="INFO") as captured:
            response = await self.submit_image()
            self.assertEqual(response.status, 200)
            self.comfy.completion.set()
            await wait_until(
                lambda: self.coordinator.status()["state"] == "llm_ready"
                and "acquire" in self.llm.calls
            )

        self.assertEqual(
            self.llm.calls,
            ["validate", "snapshot", "release", "acquire"],
        )
        self.assertEqual(self.comfy.calls, ["free", "submit", "wait", "free"])
        logs = "\n".join(captured.output)
        self.assertIn("ComfyUI idle restore timer started", logs)
        self.assertIn("ComfyUI idle timeout reached", logs)
        self.assertIn("ComfyUI idle restore completed", logs)

    async def test_idle_restore_is_disabled_by_default(self) -> None:
        await self.restart_with_config(idle_timeout=0.01)

        response = await self.submit_image()
        self.assertEqual(response.status, 200)
        self.comfy.completion.set()
        await wait_until(lambda: self.coordinator.status()["state"] == "comfy_ready")
        await asyncio.sleep(0.04)

        status = self.coordinator.status()
        self.assertFalse(status["idle_restore_enabled"])
        self.assertFalse(status["idle_restore_scheduled"])
        self.assertEqual(self.llm.calls, ["validate", "snapshot", "release"])

        async with self.coordinator.chat_lease():
            pass
        self.assertIn("acquire", self.llm.calls)

    async def test_new_work_cancels_idle_timeout(self) -> None:
        await self.restart_with_idle_timeout(0.08)

        first = await self.submit_image()
        self.assertEqual(first.status, 200)
        self.comfy.completion.set()
        await wait_until(lambda: self.coordinator.status()["state"] == "comfy_ready")
        await asyncio.sleep(0.04)

        self.comfy.completion = asyncio.Event()
        second = await self.submit_image()
        self.assertEqual(second.status, 200)
        await wait_until(lambda: self.coordinator.status()["state"] == "image_active")
        await asyncio.sleep(0.06)

        self.assertEqual(self.llm.calls, ["validate", "snapshot", "release"])
        self.comfy.completion.set()
        await wait_until(lambda: self.coordinator.status()["state"] == "comfy_ready")
        await asyncio.sleep(0.04)
        self.assertEqual(self.llm.calls, ["validate", "snapshot", "release"])
        await wait_until(
            lambda: self.coordinator.status()["state"] == "llm_ready"
            and "acquire" in self.llm.calls
        )
        self.assertEqual(self.llm.calls.count("release"), 1)
        self.assertEqual(self.llm.calls.count("acquire"), 1)

    async def test_idle_restore_readiness_failure_fails_closed(self) -> None:
        await self.restart_with_idle_timeout(0.02)
        self.llm.acquire_failures = 1

        response = await self.submit_image()
        self.assertEqual(response.status, 200)
        self.comfy.completion.set()
        await wait_until(lambda: self.coordinator.status()["state"] == "error")

        status = self.coordinator.status()
        self.assertIsNone(status["gpu_owner"])
        self.assertFalse(status["chat_available"])
        self.assertEqual(status["waiting_chats"], 0)
        self.assertEqual(status["waiting_images"], 0)
        self.assertIn("synthetic acquire failure", status["last_error"])
        self.assertEqual(
            self.llm.calls,
            ["validate", "snapshot", "release", "acquire"],
        )

    async def test_comfy_control_lease_delays_llm_restore(self) -> None:
        response = await self.submit_image()
        self.assertEqual(response.status, 200)
        self.comfy.completion.set()
        await wait_until(lambda: self.coordinator.status()["state"] == "comfy_ready")

        async def use_chat() -> None:
            async with self.coordinator.chat_lease():
                pass

        async with self.coordinator.comfy_control_lease():
            chat_task = asyncio.create_task(use_chat())
            await wait_until(lambda: self.coordinator.status()["state"] == "cleaning_comfy")
            self.assertEqual(self.coordinator.status()["active_comfy_controls"], 1)
            self.assertNotIn("acquire", self.llm.calls)

        await chat_task
        self.assertIn("acquire", self.llm.calls)
        self.assertEqual(self.coordinator.status()["active_comfy_controls"], 0)

    async def test_comfy_cleanup_failure_does_not_reload_llm(self) -> None:
        response = await self.submit_image()
        self.assertEqual(response.status, 200)
        self.comfy.completion.set()
        await wait_until(lambda: self.coordinator.status()["state"] == "comfy_ready")
        self.comfy.free_failures = 1

        with self.assertRaisesRegex(ChatUnavailable, "GPU release is not confirmed"):
            async with self.coordinator.chat_lease():
                pass

        self.assertEqual(self.coordinator.status()["state"], "error")
        self.assertNotIn("acquire", self.llm.calls)


if __name__ == "__main__":
    unittest.main()
