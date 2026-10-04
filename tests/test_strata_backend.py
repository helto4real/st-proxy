from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace

from aiohttp import ClientSession

from st_proxy.cli import check_backend
from st_proxy.config import BrokerConfig, TestEndpointRegistry
from st_proxy.errors import UpstreamError
from st_proxy.service import BrokerService

from .strata_support import MockStrata
from .support import DynamicServer, prompt_payload, wait_until
from .test_comfy_cleanup import CleanupComfy


class StrataBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.native = MockStrata()
        self.comfy = CleanupComfy()
        self.native_server = DynamicServer(self.native.app())
        self.comfy_server = DynamicServer(self.comfy.app())
        registry = TestEndpointRegistry()
        self.config = BrokerConfig.for_test(
            llm_backend='sc117',
            llm_url=registry.approve_disposable(await self.native_server.start()),
            comfy_url=registry.approve_disposable(await self.comfy_server.start()),
            registry=registry,
        )
        self.service = BrokerService(self.config)
        await self.service.start()
        self.client = ClientSession()
        self.url = f'http://127.0.0.1:{self.service.chat_port}'
        self.image_url = f'http://127.0.0.1:{self.service.image_port}'

    async def asyncTearDown(self):
        self.native.confirm.set()
        self.native.chat_release.set()
        await self.service.stop()
        await self.client.close()
        await self.native_server.close()
        await self.comfy_server.close()

    async def test_single_backend_check_catalog_and_comfy_handoff(self):
        self.assertEqual(self.service.coordinator.status()["state"], "awaiting_request")
        self.assertEqual(self.comfy.free_calls, 0)
        await check_backend(self.config, 1)
        for path in ('/health', '/v1/models', '/v1/status'):
            async with self.client.get(self.url + path) as response:
                self.assertEqual(response.status, 200)
        self.assertEqual(self.native.operations, [])
        for _ in range(2):
            async with self.client.post(self.url + '/v1/chat/completions', json={
                'messages': [], 'reasoning_effort': 'low', 'max_tokens': 2500,
            }) as response:
                self.assertEqual(response.status, 200)
                await response.read()
        self.assertEqual(self.native.operations, ['load'])
        self.assertEqual(self.native.payloads[-1], {
            'model': self.native.model, 'messages': [],
            'reasoning_effort': 'low', 'max_tokens': 2500,
        })
        async with self.client.post(self.image_url + '/prompt',
                                    json=prompt_payload()) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.native.operations, ['load', 'unload'])
        async with self.client.get(self.url + '/v1/models') as response:
            self.assertEqual(response.status, 200)
        self.assertFalse(self.native.loaded)
        async with self.client.post(self.url + '/v1/chat/completions', json={}) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.native.operations, ['load', 'unload', 'load'])

    async def test_lifecycle_settings_unknown_models_never_load(self):
        for path in ('/load', '/unload', '/v1/load', '/v1/unload', '/settings', '/models/load'):
            async with self.client.post(self.url + path, json={}) as response:
                self.assertEqual(response.status, 403)
        for model in ('unknown', 5, ''):
            async with self.client.post(self.url + '/v1/chat/completions',
                                        json={'model': model}) as response:
                self.assertEqual(response.status, 400)
        self.assertEqual(self.native.operations, [])

    async def test_false_success_does_not_confirm_gpu_transition(self):
        backend = self.service.llm
        self.native.false_confirmation = True
        with self.assertRaises(UpstreamError):
            await backend.acquire_gpu(None)
        with self.assertRaises(UpstreamError):
            await backend.observe_ready()

    async def test_timeout_keeps_unconfirmed_load_blocked(self):
        backend = self.service.llm
        backend._timeouts = replace(backend._timeouts, acquire=0.05)
        self.native.confirm.clear()
        with self.assertRaises(UpstreamError):
            await backend.acquire_gpu(None)
        with self.assertRaises(UpstreamError):
            await backend.observe_ready()

    async def test_cancellation_keeps_unconfirmed_load_blocked(self):
        backend = self.service.llm
        self.native.confirm.clear()
        task = asyncio.create_task(backend.acquire_gpu(None))
        while not self.native.operations:
            await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(UpstreamError):
            await backend.observe_ready()

    async def test_single_backend_stream_serializes_following_request(self):
        self.native.hold_chat = True
        response = await self.client.post(self.url + '/v1/chat/completions', json={
            'stream': True,
        })
        await self.native.chat_started.wait()
        sequence = self.service.coordinator._sequence
        next_request = asyncio.create_task(self.client.post(
            self.url + '/v1/chat/completions', json={'messages': []}))
        await wait_until(lambda: self.service.coordinator._sequence > sequence)
        self.assertFalse(next_request.done())
        self.assertEqual(len(self.native.payloads), 1)
        self.native.hold_chat = False
        self.native.chat_release.set()
        await response.read()
        response.close()
        async with await next_request as following:
            self.assertEqual(following.status, 200)
            await following.read()
        self.assertEqual(self.native.operations, ['load'])
        self.assertEqual(len(self.native.payloads), 2)

    async def test_injected_default_model_still_obeys_body_limit(self):
        self.service.config = replace(self.config, max_chat_body_bytes=4)
        async with self.client.post(self.url + '/v1/chat/completions', json={}) as response:
            self.assertEqual(response.status, 413)
        self.assertEqual(self.native.operations, [])
