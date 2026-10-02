from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from unittest.mock import patch

from aiohttp import ClientSession, web

from st_proxy.comfy import ComfyClient
from st_proxy.config import BrokerConfig, TestEndpointRegistry
from st_proxy.errors import UpstreamError

from .support import DynamicServer, MockComfy, wait_until


class CleanupComfy(MockComfy):
    busy = False
    changing = False
    invalid = False
    malformed = False
    queue_reads = 0
    memory_reads = 0

    async def queue(self, request):
        self.queue_reads += 1
        if self.busy:
            return web.json_response({"queue_running": [["synthetic"]], "queue_pending": []})
        return await super().queue(request)

    async def system_stats(self, request):
        self.memory_reads += 1
        if self.malformed:
            return web.Response(text="not-json", content_type="application/json")
        if self.invalid:
            return web.json_response({"devices": []})
        if self.changing:
            return web.json_response({"devices": [{
                "vram_total": 24000, "vram_free": 22000,
                # Reported free memory stays equal while driver-free memory changes.
                "torch_vram_total": 1000, "torch_vram_free": self.memory_reads % 2,
            }]})
        return await super().system_stats(request)


class ComfyCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.comfy = CleanupComfy()
        self.server = DynamicServer(self.comfy.app())
        origin = await self.server.start()
        registry = TestEndpointRegistry()
        registry.approve_disposable(origin)
        self.config = BrokerConfig.for_test(
            llm_backend="tabbyapi", llm_url=origin, comfy_url=origin,
            registry=registry, tabby_model="synthetic", poll_interval=0.005,
        )
        self.session = ClientSession()
        self.client = ComfyClient(self.session, self.config)
        real_sleep = asyncio.sleep

        async def skip_fixed_wait(delay):
            await real_sleep(0 if delay == 5.0 else delay)

        # The existing integration test verifies the real five-second minimum.
        self.sleep_patch = patch("st_proxy.comfy.asyncio.sleep", side_effect=skip_fixed_wait)
        self.sleep_patch.start()

    async def asyncTearDown(self):
        self.sleep_patch.stop()
        await self.session.close()
        await self.server.close()

    async def test_cleanup_waits_for_queue_and_memory_to_settle(self):
        self.comfy.busy = True
        self.comfy.changing = True
        task = asyncio.create_task(self.client.free_models())
        try:
            await wait_until(lambda: self.comfy.queue_reads >= 2)
            self.assertEqual(self.comfy.free_calls, 0)
            self.comfy.busy = False
            await wait_until(lambda: self.comfy.memory_reads >= 3)
            self.assertEqual(self.comfy.free_calls, 1)
            self.assertFalse(task.done())
            reads = self.comfy.memory_reads
            self.comfy.changing = False
            await task
            self.assertGreaterEqual(self.comfy.memory_reads - reads, 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_invalid_status_and_unsettled_memory_fail_then_can_recover(self):
        self.client = ComfyClient(self.session, replace(self.config, cleanup_timeout=0.05))
        for flag in ("invalid", "malformed", "changing", "busy"):
            with self.subTest(flag=flag):
                setattr(self.comfy, flag, True)
                with self.assertRaises(UpstreamError):
                    await self.client.free_models()
                setattr(self.comfy, flag, False)
                await self.client.free_models()
