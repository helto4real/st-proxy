from __future__ import annotations

import tempfile
import unittest

from aiohttp import ClientSession

from st_proxy.cli import check_backend
from st_proxy.config import BrokerConfig, TestEndpointRegistry
from st_proxy.service import BrokerService

from .support import DynamicServer, MockComfy, MockOllama, prompt_payload, wait_until


class OllamaBackendTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="st-proxy-ollama-test-")
        self.ollama = MockOllama()
        self.comfy = MockComfy()
        self.ollama_server = DynamicServer(self.ollama.app())
        self.comfy_server = DynamicServer(self.comfy.app())
        ollama_url = await self.ollama_server.start()
        comfy_url = await self.comfy_server.start()
        self.registry = TestEndpointRegistry()
        self.registry.approve_disposable(ollama_url)
        self.registry.approve_disposable(comfy_url)
        self.config = BrokerConfig.for_test(
            llm_backend="ollama",
            llm_url=ollama_url,
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
        await self.ollama_server.close()
        self.tempdir.cleanup()

    @property
    def chat_url(self) -> str:
        return f"http://127.0.0.1:{self.service.chat_port}"

    @property
    def image_url(self) -> str:
        return f"http://127.0.0.1:{self.service.image_port}"

    async def status(self) -> dict:
        async with self.client.get(f"{self.chat_url}/broker/status") as response:
            self.assertEqual(response.status, 200)
            return await response.json()

    async def test_complete_handoff_uses_ollama_lifecycle_api(self) -> None:
        async with self.client.post(
            f"{self.image_url}/prompt",
            json=prompt_payload(),
        ) as response:
            self.assertEqual(response.status, 200)
        await wait_until(
            lambda: (
                self.service.coordinator is not None
                and self.service.coordinator.status()["state"] == "comfy_ready"
            )
        )
        self.assertEqual(
            self.ollama.lifecycle_calls,
            [("synthetic-model:latest", 0)],
        )
        self.assertEqual((await self.status())["llm_backend"], "ollama")

        async with self.client.post(
            f"{self.chat_url}/api/chat",
            json={"model": "synthetic-model:latest", "messages": []},
        ) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(
            self.ollama.lifecycle_calls,
            [
                ("synthetic-model:latest", 0),
                ("synthetic-model:latest", -1),
            ],
        )
        self.assertEqual(self.ollama.chat_requests, 1)
        self.assertEqual((await self.status())["state"], "llm_ready")

    async def test_startup_fails_closed_when_multiple_models_are_loaded(self) -> None:
        await self.service.stop()
        self.ollama.loaded_models.append("other-model:latest")
        self.service = BrokerService(self.config)
        await self.service.start()

        status = await self.status()
        self.assertEqual(status["state"], "error")
        self.assertFalse(status["chat_available"])
        self.assertIn("exactly one loaded model", status["last_error"])

    async def test_unload_failure_does_not_restore_ollama(self) -> None:
        self.ollama.unload_failures = 1
        async with self.client.post(
            f"{self.image_url}/prompt",
            json=prompt_payload(),
        ) as response:
            self.assertEqual(response.status, 503)

        await wait_until(
            lambda: (
                self.service.coordinator is not None
                and self.service.coordinator.status()["state"] == "error"
            )
        )
        self.assertEqual(
            self.ollama.lifecycle_calls,
            [
                ("synthetic-model:latest", 0),
            ],
        )
        self.assertEqual(self.ollama.loaded_models, ["synthetic-model:latest"])

    async def test_backend_check_reuses_adapter_readiness_contract(self) -> None:
        await check_backend(self.config, 1)


if __name__ == "__main__":
    unittest.main()
