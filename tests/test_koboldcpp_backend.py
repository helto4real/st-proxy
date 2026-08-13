from __future__ import annotations

import unittest

from aiohttp import ClientSession

from st_proxy.llm import BackendTimeouts
from st_proxy.llm.koboldcpp import KoboldCppBackend

from .support import DynamicServer, MockKobold


class KoboldCppAdapterContractTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.kobold = MockKobold()
        self.server = DynamicServer(self.kobold.app())
        self.origin = await self.server.start()
        self.session = ClientSession()
        self.backend = KoboldCppBackend(
            self.session,
            origin=self.origin,
            admin_password="synthetic-secret",
            timeouts=BackendTimeouts(
                request=1,
                release=1,
                acquire=1,
                poll_interval=0.01,
            ),
        )

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.server.close()

    async def test_adapter_satisfies_lifecycle_contract(self) -> None:
        await self.backend.validate_control()
        restore_point = await self.backend.snapshot_ready()
        await self.backend.release_gpu(restore_point)
        await self.backend.acquire_gpu(restore_point)

        self.assertEqual(restore_point.model, "synthetic-model.gguf")
        self.assertEqual(
            self.kobold.admin_calls,
            ["unload_model", "initial_model"],
        )
        self.assertEqual(
            self.kobold.admin_authorization,
            ["Bearer synthetic-secret", "Bearer synthetic-secret"],
        )
        self.assertEqual(self.kobold.model, restore_point.model)

    async def test_observation_is_passive_and_default_load_returns_restore_point(
        self,
    ) -> None:
        observed = await self.backend.observe_ready()
        self.assertIsNotNone(observed)
        self.assertEqual(self.kobold.admin_calls, [])

        self.kobold.model = "inactive"
        self.assertIsNone(await self.backend.observe_ready())
        self.assertEqual(self.kobold.admin_calls, [])

        restored = await self.backend.acquire_gpu(None)
        self.assertEqual(restored.model, "synthetic-model.gguf")
        self.assertEqual(self.kobold.admin_calls, ["initial_model"])


if __name__ == "__main__":
    unittest.main()
