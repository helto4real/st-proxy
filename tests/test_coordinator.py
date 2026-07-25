from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass, field

from st_proxy.comfy import PromptResult
from st_proxy.config import BrokerConfig
from st_proxy.coordinator import HandoffCoordinator
from st_proxy.errors import HandoffError, UpstreamError
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


@dataclass
class FakeComfyClient:
    calls: list[str] = field(default_factory=list)
    completion: asyncio.Event = field(default_factory=asyncio.Event)

    async def free_models(self) -> None:
        self.calls.append("free")

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

    async def test_release_failure_restores_generic_backend(self) -> None:
        self.llm.release_failures = 1

        with self.assertRaisesRegex(HandoffError, "synthetic release failure"):
            await self.submit_image()

        await wait_until(lambda: self.coordinator.status()["state"] == "llm_ready")
        self.assertEqual(
            self.llm.calls,
            ["validate", "snapshot", "release", "acquire"],
        )


if __name__ == "__main__":
    unittest.main()
