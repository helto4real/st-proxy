from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace

from aiohttp import ClientSession, web

from st_proxy.cli import check_backend
from st_proxy.config import BrokerConfig, TestEndpointRegistry
from st_proxy.errors import UpstreamError
from st_proxy.llm.base import BackendTimeouts
from st_proxy.llm.prism import PrismBackend, PrismRestorePoint
from st_proxy.service import BrokerService

from .support import DynamicServer, MockComfy, MockKobold, prompt_payload, wait_until


class MockPrism(MockKobold):
    def __init__(self):
        super().__init__()
        self.model = "synthetic-prism"
        self.other = "synthetic-alternative"
        self.states = {self.model: "unloaded", self.other: "unloaded"}
        self.operations = []
        self.fail_load = None
        self.commands = []
        self.pending_state = None
        self.confirm = asyncio.Event()
        self.confirm.set()
        self.fail_unload = False
        self.extra_model = False
        self.valid_schema = True

    @property
    def state(self):
        return self.states[self.model]

    @state.setter
    def state(self, value):
        self.states[self.model] = value

    def app(self):
        app = web.Application()
        for path in ("/models", "/v1/models"):
            app.router.add_get(path, self.models)
        app.router.add_get("/health", self.health)
        app.router.add_get("/props", self.props)
        app.router.add_post("/models/load", self.load)
        app.router.add_post("/models/unload", self.unload)
        app.router.add_post("/v1/chat/completions", self.chat)
        return app

    async def health(self, request):
        self.request_paths.append(request.path)
        return web.json_response({"status": "ok"})

    async def props(self, request):
        self.request_paths.append(request.path)
        model = request.query.get("model")
        assert self.states[model] == "loaded"
        return web.json_response({"is_sleeping": False, "model": model})

    async def models(self, request):
        self.request_paths.append(request.path)
        if self.confirm.is_set() and self.pending_state:
            model, state = self.pending_state
            self.states[model] = state
            self.pending_state = None
        models = []
        for model, value in self.states.items():
            card = {"id": model}
            if self.valid_schema:
                card["status"] = {"value": value}
                if self.fail_load == model:
                    card["status"]["failed"] = True
            models.append(card)
        if self.extra_model:
            models.append({"id": "other", "status": {"value": "loaded"}})
        return web.json_response({"object": "list", "data": models})

    async def load(self, request):
        model = (await request.json())["model"]
        assert model in self.states
        assert all(state == "unloaded" for state in self.states.values())
        self.commands.append("load")
        self.operations.append(("load", model))
        if model != self.fail_load:
            self.states[model] = "loading"
            self.pending_state = (model, "loaded")
        return web.json_response({"success": True})

    async def unload(self, request):
        model = (await request.json())["model"]
        assert model in self.states
        self.commands.append("unload")
        self.operations.append(("unload", model))
        if self.fail_unload:
            return web.json_response({"success": False}, status=500)
        self.pending_state = (model, "unloaded")
        return web.json_response({"success": True})

    async def chat(self, request):
        payload = await request.read()
        assert self.states[json.loads(payload)["model"]] == "loaded"
        self.chat_payloads.append(payload)
        if json.loads(payload).get("stream"):
            return await self.stream(request)
        self.chat_requests += 1
        return web.json_response({"choices": [{"message": {
            "role": "assistant", "content": "synthetic", "reasoning_content": "thinking",
        }}]})


class PrismTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.prism = MockPrism()
        self.comfy = MockComfy()
        self.server = DynamicServer(self.prism.app())
        self.comfy_server = DynamicServer(self.comfy.app())
        origin = await self.server.start()
        comfy_origin = await self.comfy_server.start()
        registry = TestEndpointRegistry()
        registry.approve_disposable(origin)
        registry.approve_disposable(comfy_origin)
        self.config = BrokerConfig.for_test(
            llm_backend="prism", llm_url=origin, comfy_url=comfy_origin,
            registry=registry, prism_model=self.prism.model,
        )
        self.client = ClientSession()
        self.backend = PrismBackend(
            self.client, origin=origin, model=self.prism.model,
            timeouts=BackendTimeouts(1, 1, 1, 0.01),
        )
        self.service = BrokerService(self.config)
        await self.service.start()
        self.chat_url = f"http://127.0.0.1:{self.service.chat_port}"
        self.image_url = f"http://127.0.0.1:{self.service.image_port}"

    async def asyncTearDown(self):
        self.prism.chat_release.set()
        self.prism.confirm.set()
        for event in self.comfy.completion.values():
            event.set()
        await self.service.stop()
        await self.client.close()
        await self.server.close()
        await self.comfy_server.close()

    async def image(self):
        async with self.client.post(self.image_url+"/prompt", json=prompt_payload()) as response:
            return response.status

    async def chat(self, **payload):
        async with self.client.post(self.chat_url+"/v1/chat/completions", json=payload) as response:
            return response.status, await response.json()

    async def test_startup_check_and_discovery_never_load(self):
        self.assertEqual(self.prism.request_paths, [])
        await check_backend(self.config, 1)
        for path in ("/health", "/models", "/v1/models"):
            for method in ("GET", "HEAD"):
                async with self.client.request(method, self.chat_url+path) as response:
                    self.assertEqual(response.status, 200)
                    await response.read()
        async with self.client.get(self.chat_url+"/props") as response:
            self.assertEqual(response.status, 503)
        self.assertEqual(self.prism.commands, [])
        self.assertEqual(self.comfy.free_calls, 0)
        self.assertEqual(self.service.coordinator.status()["state"], "awaiting_request")

    async def test_adapter_confirms_both_transitions_after_http_success(self):
        self.prism.confirm.clear()
        task = asyncio.create_task(self.backend.acquire_gpu(None))
        await wait_until(lambda: self.prism.commands == ["load"])
        self.assertFalse(task.done())
        self.prism.confirm.set()
        target = await task
        self.prism.confirm.clear()
        task = asyncio.create_task(self.backend.release_gpu(target))
        await wait_until(lambda: self.prism.commands == ["load", "unload"])
        self.assertFalse(task.done())
        self.prism.confirm.set()
        await task
        self.assertIsNone(await self.backend.observe_ready())
        self.assertEqual(await self.backend.acquire_gpu(target), target)

    async def test_unconfirmed_mutation_stays_fail_closed(self):
        self.prism.confirm.clear()
        self.backend._timeouts = replace(self.backend._timeouts, acquire=0.04)
        with self.assertRaises(UpstreamError):
            await self.backend.acquire_gpu(None)
        self.prism.confirm.set()
        self.prism.state = "unloaded"
        self.prism.pending_state = None
        with self.assertRaisesRegex(UpstreamError, "previous load/unload"):
            await self.backend.observe_ready()
        with self.assertRaises(UpstreamError):
            await self.backend.release_gpu(PrismRestorePoint(self.prism.model))

    async def test_invalid_or_ambiguous_router_state_is_rejected(self):
        self.prism.valid_schema = False
        with self.assertRaises(UpstreamError):
            await self.backend.validate_control()
        self.prism.valid_schema = True
        self.prism.extra_model = True
        self.prism.state = "loaded"
        with self.assertRaises(UpstreamError):
            await self.backend.validate_control()
        self.prism.extra_model = False
        self.prism.state = "loading"
        with self.assertRaises(UpstreamError):
            await self.backend.observe_ready()
        with self.assertRaises(UpstreamError):
            await self.backend.acquire_gpu(PrismRestorePoint("wrong-preset"))

    async def test_handoff_restores_only_for_chat_and_preserves_reasoning(self):
        payload = {"model": self.prism.model, "reasoning_effort": "medium",
                   "reasoning_budget_tokens": 128, "max_tokens": 256}
        status, body = await self.chat(**payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["choices"][0]["message"]["reasoning_content"], "thinking")
        self.assertEqual(json.loads(self.prism.chat_payloads[0]), payload)
        self.assertEqual(self.prism.commands, ["load"])
        self.assertEqual(await self.image(), 200)
        await wait_until(lambda: self.service.coordinator.status()["state"] == "comfy_ready")
        self.assertEqual(self.prism.commands, ["load", "unload"])
        self.assertEqual(await self.image(), 200)
        await wait_until(lambda: self.service.coordinator.status()["state"] == "comfy_ready")
        async with self.client.get(self.chat_url+"/v1/models") as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.prism.commands, ["load", "unload"])
        status, _ = await self.chat(**payload)
        self.assertEqual(status, 200)
        self.assertEqual(self.prism.commands, ["load", "unload", "load"])

    async def test_image_first_does_not_load_and_failed_unload_blocks_image(self):
        self.assertEqual(await self.image(), 200)
        self.assertEqual(self.prism.commands, [])
        await wait_until(lambda: self.service.coordinator.status()["state"] == "comfy_ready")
        status, _ = await self.chat(model=self.prism.model)
        self.assertEqual(status, 200)
        self.prism.fail_unload = True
        self.assertEqual(await self.image(), 503)
        self.assertEqual(len(self.comfy.prompt_calls), 1)

    async def test_stream_drains_before_model_is_unloaded(self):
        self.prism.hold_chat = True
        response = await self.client.post(self.chat_url+"/v1/chat/completions", json={
            "model": self.prism.model, "stream": True,
        })
        await self.prism.chat_started.wait()
        task = asyncio.create_task(self.image())
        await wait_until(lambda: self.service.coordinator.status()["state"] == "draining_llm")
        self.assertEqual(self.prism.commands, ["load"])
        self.prism.chat_release.set()
        self.assertEqual(await response.read(), b"data: synthetic-one\n\ndata: synthetic-two\n\n")
        response.close()
        self.assertEqual(await task, 200)
        self.assertEqual(self.prism.commands, ["load", "unload"])

    async def test_lifecycle_bypasses_are_blocked_without_loading(self):
        for method, path in (
            ("POST", "/models/load"), ("POST", "/models/unload"),
            ("POST", "/models"), ("DELETE", "/models"), ("POST", "/props"),
            ("GET", "/models?reload=1"), ("GET", "/v1/models?reload=1"),
            ("GET", "/props?autoload=true"),
        ):
            with self.subTest(path=path, method=method):
                async with self.client.request(method, self.chat_url+path) as response:
                    self.assertEqual(response.status, 403)
        self.assertEqual(self.prism.commands, [])
        self.assertEqual(self.comfy.free_calls, 0)


    async def test_model_selection_cold_switch_reuse_and_default(self):
        async with self.client.get(self.chat_url+"/v1/models") as response:
            self.assertEqual({card["id"] for card in (await response.json())["data"]},
                             {self.prism.model, self.prism.other})
        self.assertEqual(self.prism.operations, [])
        for model in (self.prism.other, self.prism.other, self.prism.model):
            status, _ = await self.chat(model=model)
            self.assertEqual(status, 200)
        self.assertEqual(self.prism.operations, [
            ("load", self.prism.other), ("unload", self.prism.other),
            ("load", self.prism.model),
        ])
        self.assertEqual(self.comfy.free_calls, 1)
        self.assertEqual((await self.chat())[0], 200)
        self.assertEqual(json.loads(self.prism.chat_payloads[-1])["model"], self.prism.model)
        self.assertEqual(len(self.prism.operations), 3)

    async def test_invalid_model_and_body_rejected_before_gpu_acquisition(self):
        for model in ("unknown", "", 1, {}, []):
            with self.subTest(model=model):
                async with self.client.post(self.chat_url+"/v1/chat/completions",
                                            json={"model": model}) as response:
                    self.assertEqual(response.status, 400)
        for body in (b"broken", b"[]"):
            async with self.client.post(self.chat_url+"/v1/chat/completions",
                                        data=body) as response:
                self.assertEqual(response.status, 400)
        async with self.client.post(self.chat_url+"/v1/chat/completions?model=other",
                                    json={"model": self.prism.model}) as response:
            self.assertEqual(response.status, 400)
        self.assertEqual(self.prism.operations, [])
        self.assertEqual(self.comfy.free_calls, 0)

    async def test_alternate_model_handoff_and_direct_selection_after_image(self):
        self.assertEqual((await self.chat(model=self.prism.other))[0], 200)
        self.assertEqual(await self.image(), 200)
        await wait_until(lambda: self.service.coordinator.status()["state"] == "comfy_ready")
        self.assertEqual(self.prism.operations, [
            ("load", self.prism.other), ("unload", self.prism.other),
        ])
        async with self.client.get(self.chat_url+"/v1/models") as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(len(self.prism.operations), 2)
        self.assertEqual((await self.chat(model=self.prism.model))[0], 200)
        self.assertEqual(self.prism.operations[-1], ("load", self.prism.model))
        self.assertEqual(len(self.prism.operations), 3)

    async def test_metadata_observes_active_model_without_switching(self):
        self.assertEqual((await self.chat(model=self.prism.other))[0], 200)
        async with self.client.get(self.chat_url+"/props") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["model"], self.prism.other)
        async with self.client.get(self.chat_url+"/props", params={
            "model": self.prism.other, "unrelated": "keep me",
        }) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["model"], self.prism.other)
        async with self.client.get(self.chat_url+"/props", params={
            "model": self.prism.model,
        }) as response:
            self.assertEqual(response.status, 503)
        self.assertEqual(self.prism.operations, [("load", self.prism.other)])

    async def test_switch_waits_for_stream_then_preserves_image_fifo(self):
        self.prism.hold_chat = True
        response = await self.client.post(self.chat_url+"/v1/chat/completions", json={
            "model": self.prism.model, "stream": True,
        })
        await self.prism.chat_started.wait()
        sequence = self.service.coordinator._sequence
        switch = asyncio.create_task(self.chat(model=self.prism.other))
        await wait_until(lambda: self.service.coordinator._sequence > sequence)
        image = asyncio.create_task(self.image())
        await wait_until(lambda: self.service.coordinator.status()["waiting_images"] == 1)
        self.assertFalse(switch.done())
        self.assertEqual(self.prism.operations, [("load", self.prism.model)])
        self.prism.chat_release.set()
        await response.read()
        response.close()
        self.assertEqual((await switch)[0], 200)
        self.assertEqual(await image, 200)
        self.assertEqual(self.prism.operations, [
            ("load", self.prism.model), ("unload", self.prism.model),
            ("load", self.prism.other), ("unload", self.prism.other),
        ])

    async def test_failed_switch_unload_never_loads_alternative_or_submits_image(self):
        self.assertEqual((await self.chat(model=self.prism.model))[0], 200)
        self.prism.fail_unload = True
        self.assertEqual((await self.chat(model=self.prism.other))[0], 503)
        self.assertEqual(await self.image(), 503)
        self.assertEqual(self.prism.operations, [
            ("load", self.prism.model), ("unload", self.prism.model),
        ])
        self.assertEqual(self.comfy.prompt_calls, [])

    async def test_failed_switch_load_keeps_gpu_ownership_unconfirmed(self):
        self.assertEqual((await self.chat(model=self.prism.model))[0], 200)
        self.prism.fail_load = self.prism.other
        self.assertEqual((await self.chat(model=self.prism.other))[0], 503)
        self.assertEqual(await self.image(), 503)
        self.assertEqual(self.prism.operations, [
            ("load", self.prism.model), ("unload", self.prism.model),
            ("load", self.prism.other),
        ])
        self.assertEqual(self.comfy.prompt_calls, [])

    async def test_switch_requires_confirmed_unload_before_loading_next(self):
        await self.backend.acquire_gpu(None)
        self.prism.confirm.clear()
        task = asyncio.create_task(self.backend.acquire_gpu(PrismRestorePoint(self.prism.other)))
        await wait_until(lambda: self.prism.commands == ["load", "unload"])
        self.assertFalse(task.done())
        self.assertEqual(self.prism.states[self.prism.other], "unloaded")
        self.prism.confirm.set()
        self.assertEqual(await task, PrismRestorePoint(self.prism.other))
        self.assertEqual(await self.backend.snapshot_ready(), PrismRestorePoint(self.prism.other))

    async def test_preexisting_alternative_is_unloaded_for_image_first(self):
        self.prism.states[self.prism.other] = "loaded"
        self.assertEqual(await self.image(), 200)
        self.assertEqual(self.prism.operations, [("unload", self.prism.other)])

    async def test_disconnected_waiting_switch_does_not_change_model(self):
        coordinator = self.service.coordinator
        dispatch = coordinator._dispatch_chat
        works = []

        async def observe_dispatch(work):
            works.append(work)
            await dispatch(work)

        coordinator._dispatch_chat = observe_dispatch
        self.prism.hold_chat = True
        response = await self.client.post(self.chat_url+"/v1/chat/completions", json={
            "model": self.prism.model, "stream": True,
        })
        await self.prism.chat_started.wait()
        sequence = self.service.coordinator._sequence
        async with ClientSession() as other_client:
            task = asyncio.create_task(other_client.post(self.chat_url+"/v1/chat/completions",
                json={"model": self.prism.other}))
            await wait_until(lambda: self.service.coordinator._sequence > sequence)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # Observe cancellation before letting the original stream finish.
        await wait_until(lambda: any(
            work.target == PrismRestorePoint(self.prism.other) and work.cancelled
            for work in works
        ))
        self.prism.chat_release.set()
        await response.read()
        response.close()
        self.assertEqual((await self.chat(model=self.prism.model))[0], 200)
        self.assertEqual(self.prism.operations, [("load", self.prism.model)])
