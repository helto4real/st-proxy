from __future__ import annotations

import asyncio
import itertools
import json
import unittest
from dataclasses import replace

from aiohttp import ClientSession, web

from st_proxy.cli import check_backend
from st_proxy.config import BrokerConfig, TestEndpointRegistry
from st_proxy.errors import ConfigurationError
from st_proxy.service import BrokerService

from .strata_support import MockStrata
from .support import DynamicServer, prompt_payload, wait_until
from .tabby_support import MockTabby
from .test_comfy_cleanup import CleanupComfy
from .test_kobold_router import NativeRouter
from .test_prism_backend import MockPrism


class MultiBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.kobold = NativeRouter()
        self.kobold.model = "inactive"
        self.kobold.current_profile = "unload_model"
        self.tabby = MockTabby()
        self.tabby.model = "inactive"
        self.prism = MockPrism()
        self.sc117 = MockStrata("synthetic-sc117")
        self.orca = MockStrata("synthetic-orca")
        self.comfy = CleanupComfy()
        self.servers = []
        self.events = []
        registry = TestEndpointRegistry()
        origins = {}
        for engine, mock in (("koboldcpp", self.kobold), ("tabbyapi", self.tabby),
                             ("prism", self.prism), ("sc117", self.sc117),
                             ("orca", self.orca), ("comfy", self.comfy)):
            app = mock.app()
            app.middlewares.append(self.guard(engine))
            server = DynamicServer(app)
            self.servers.append(server)
            origins[engine] = registry.approve_disposable(await server.start())
        self.config = BrokerConfig.for_test(
            llm_backend="all", llm_url=origins["koboldcpp"], tabby_url=origins["tabbyapi"],
            prism_url=origins["prism"], comfy_url=origins["comfy"], registry=registry,
            prism_model=self.prism.model, sc117_url=origins["sc117"], orca_url=origins["orca"],
        )
        self.service = BrokerService(self.config)
        await self.service.start()
        self.client = ClientSession()
        self.url = f"http://127.0.0.1:{self.service.chat_port}"
        self.image_url = f"http://127.0.0.1:{self.service.image_port}"
        self.models = {"koboldcpp": "koboldcpp/a.kcpps", "tabbyapi": "tabbyapi/synthetic-exl3",
                       "prism": f"prism/{self.prism.model}",
                       "sc117": f"sc117/{self.sc117.model}", "orca": f"orca/{self.orca.model}"}

    def guard(self, engine):
        @web.middleware
        async def middleware(request, handler):
            self.events.append((engine, request.method, request.path))
            if request.path in {"/v1/chat/completions", "/v1/model/load", "/models/load",
                                "/v1/load", "/prompt"}:
                if engine != "koboldcpp":
                    self.assertEqual(self.kobold.model, "inactive")
                if engine != "tabbyapi":
                    self.assertEqual(self.tabby.model, "inactive")
                if engine != "prism":
                    self.assertTrue(all(s == "unloaded" for s in self.prism.states.values()))
                if engine != "sc117":
                    self.assertFalse(self.sc117.loaded)
                if engine != "orca":
                    self.assertFalse(self.orca.loaded)
                if engine != "comfy":
                    self.assertTrue(all(event.is_set() for event in self.comfy.completion.values()))
            return await handler(request)
        return middleware

    async def asyncTearDown(self):
        for mock in (self.kobold, self.tabby, self.prism, self.sc117, self.orca):
            mock.chat_release.set()
        self.kobold.load_gate.set()
        self.tabby.load_release.set()
        self.prism.confirm.set()
        self.sc117.confirm.set()
        self.orca.confirm.set()
        for event in self.comfy.completion.values():
            event.set()
        await self.service.stop()
        await self.client.close()
        for server in self.servers:
            await server.close()

    async def chat(self, model, **extra):
        async with self.client.post(self.url + "/v1/chat/completions", json={
            "model": model, "messages": [{"role": "user", "content": "synthetic"}], **extra,
        }) as response:
            return response.status, await response.read()

    async def image(self):
        async with self.client.post(self.image_url + "/prompt", json=prompt_payload()) as response:
            await response.read()
            return response.status

    async def test_catalog_and_check_are_passive_and_preserve_native_ids(self):
        self.assertEqual(self.events, [])
        await check_backend(self.config, 1)
        async with self.client.get(self.url + "/v1/models") as response:
            self.assertEqual(response.status, 200)
            cards = (await response.json())["data"]
        expected = {"koboldcpp/initial_model", "koboldcpp/a.kcpps", "koboldcpp/b.kcpps",
                    "tabbyapi/synthetic-exl3", "tabbyapi/another-exl3",
                    f"prism/{self.prism.model}", f"prism/{self.prism.other}",
                    f"sc117/{self.sc117.model}", f"orca/{self.orca.model}"}
        self.assertEqual({card["id"] for card in cards}, expected)
        self.assertFalse(any(method == "POST" for _, method, _ in self.events))
        self.assertEqual(self.service.coordinator.status()["state"], "awaiting_request")
        # Discovery is native, not a hard-coded list or a scan of local files.
        self.tabby.available_models.append("new/native-model")
        async with self.client.get(self.url + "/v1/models") as response:
            self.assertIn("tabbyapi/new/native-model",
                          {card["id"] for card in (await response.json())["data"]})

    async def test_all_twenty_cross_engine_directions_and_native_model_translation(self):
        for source, destination in itertools.permutations(self.models, 2):
            with self.subTest(source=source, destination=destination):
                self.assertEqual((await self.chat(self.models[source]))[0], 200)
                self.assertEqual((await self.chat(self.models[destination]))[0], 200)
                self.assertEqual(self.service.llm.active.engine, destination)
        self.assertEqual(set(self.kobold.selected), {"a.kcpps"})
        self.assertEqual({json.loads(body)["model"] for body in self.tabby.payloads},
                         {"synthetic-exl3"})
        self.assertEqual({json.loads(body)["model"] for body in self.prism.chat_payloads},
                         {self.prism.model})
        for mock in (self.sc117, self.orca):
            self.assertEqual({payload["model"] for payload in mock.payloads}, {mock.model})
        self.assertEqual(self.comfy.free_calls, 1)

    async def test_each_engine_to_comfy_and_next_engine_without_background_restore(self):
        for index, engine in enumerate(self.models):
            with self.subTest(engine=engine):
                self.assertEqual((await self.chat(self.models[engine]))[0], 200)
                self.assertEqual(await self.image(), 200)
                await wait_until(
                    lambda: self.service.coordinator.status()["state"] == "comfy_ready")
                before = [event for event in self.events if event[1] == "POST"]
                async with self.client.get(self.url + "/v1/models") as response:
                    self.assertEqual(response.status, 200)
                await asyncio.sleep(0.02)
                self.assertEqual([event for event in self.events if event[1] == "POST"], before)
                next_engine = list(self.models)[(index + 1) % len(self.models)]
                self.assertEqual((await self.chat(self.models[next_engine]))[0], 200)

    async def test_stream_blocks_switch_and_preserves_fifo_with_image(self):
        self.tabby.hold_chat = True
        response = await self.client.post(self.url + "/v1/chat/completions", json={
            "model": self.models["tabbyapi"], "stream": True,
        })
        await self.tabby.chat_started.wait()
        sequence = self.service.coordinator._sequence
        switch = asyncio.create_task(self.chat(self.models["prism"]))
        await wait_until(lambda: self.service.coordinator._sequence > sequence)
        image = asyncio.create_task(self.image())
        await wait_until(lambda: self.service.coordinator.status()["waiting_images"] == 1)
        self.assertEqual(self.tabby.unloads, 0)
        self.assertEqual(self.prism.operations, [])
        self.assertFalse(switch.done())
        self.tabby.chat_release.set()
        self.assertEqual(await response.read(), b"data: synthetic-one\n\ndata: synthetic-two\n\n")
        response.close()
        self.assertEqual((await switch)[0], 200)
        self.assertEqual(await image, 200)
        self.assertEqual(self.prism.operations, [("load", self.prism.model),
                                                ("unload", self.prism.model)])

    async def test_failed_unload_blocks_next_engine_and_image(self):
        self.assertEqual((await self.chat(self.models["prism"]))[0], 200)
        self.prism.fail_unload = True
        self.assertEqual((await self.chat(self.models["tabbyapi"]))[0], 503)
        self.assertEqual(await self.image(), 503)
        self.assertEqual(self.tabby.loads, [])
        self.assertEqual(self.comfy.prompt_calls, [])

    async def test_failed_load_blocks_other_engine_and_image_but_same_tabby_can_retry(self):
        self.tabby.load_error = True
        self.assertEqual((await self.chat(self.models["tabbyapi"]))[0], 503)
        self.assertEqual((await self.chat(self.models["prism"]))[0], 503)
        self.assertEqual(await self.image(), 503)
        self.assertEqual(self.prism.operations, [])
        self.assertEqual(self.comfy.prompt_calls, [])
        self.tabby.load_error = False
        self.assertEqual((await self.chat(self.models["tabbyapi"]))[0], 200)

    async def test_unload_confirmation_precedes_next_engine_load(self):
        self.assertEqual((await self.chat(self.models["prism"]))[0], 200)
        self.prism.confirm.clear()
        task = asyncio.create_task(self.chat(self.models["tabbyapi"]))
        await wait_until(lambda: self.prism.commands == ["load", "unload"])
        self.assertEqual(self.tabby.loads, [])
        self.assertFalse(task.done())
        self.prism.confirm.set()
        self.sc117.confirm.set()
        self.orca.confirm.set()
        self.assertEqual((await task)[0], 200)

    async def test_tabby_failed_switch_can_retry_the_requested_model(self):
        self.assertEqual((await self.chat(self.models["tabbyapi"]))[0], 200)
        self.tabby.load_error = True
        self.assertEqual((await self.chat("tabbyapi/another-exl3"))[0], 503)
        self.assertIsNone(self.service.llm.active)
        self.assertEqual((await self.chat(self.models["prism"]))[0], 503)
        self.tabby.load_error = False
        self.assertEqual((await self.chat("tabbyapi/another-exl3"))[0], 200)
        self.assertEqual(self.tabby.model, "another-exl3")
        self.assertEqual(self.prism.operations, [])

    async def test_same_model_reuses_load_and_tabby_settings_survive_switch(self):
        self.tabby.model_overrides["synthetic-exl3"] = {"max_seq_len": 16384}
        self.assertEqual((await self.chat(self.models["tabbyapi"],
                                         reasoning_effort="low", max_tokens=100))[0], 200)
        self.assertEqual(json.loads(self.tabby.payloads[-1])["reasoning_budget_tokens"], 25)
        self.assertEqual((await self.chat(self.models["tabbyapi"]))[0], 200)
        self.assertEqual(len(self.tabby.loads), 1)
        self.assertEqual((await self.chat("tabbyapi/another-exl3"))[0], 200)
        self.assertEqual((await self.chat(self.models["prism"]))[0], 200)
        self.assertEqual((await self.chat(self.models["tabbyapi"]))[0], 200)
        self.assertEqual(self.tabby.loads[-1]["max_seq_len"], 16384)
        self.assertEqual(self.tabby.loads[-1]["cache_mode"], "Q4")

    async def test_image_first_releases_all_preexisting_models(self):
        self.kobold.model = "preexisting"
        self.tabby.model = "synthetic-exl3"
        self.prism.states[self.prism.other] = "loaded"
        self.sc117.loaded = self.orca.loaded = True
        self.assertEqual(await self.image(), 200)
        self.assertEqual(self.kobold.admin_calls, ["unload_model"])
        self.assertEqual(self.tabby.unloads, 1)
        self.assertEqual(self.prism.operations, [("unload", self.prism.other)])

    async def test_unknown_missing_models_and_lifecycle_bypasses_do_not_acquire_gpu(self):
        for model in (None, "", "synthetic-exl3", "tabbyapi/missing", "wrong/a", 5):
            with self.subTest(model=model):
                self.assertEqual((await self.chat(model))[0], 400)
        for path in ("/models/load", "/v1/model/load", "/api/admin/reload_config", "/props"):
            async with self.client.post(self.url + path,
                                        json={"model": self.models["prism"]}) as response:
                self.assertEqual(response.status, 403)
        async with self.client.get(self.url + "/v1/models?reload=1") as response:
            self.assertEqual(response.status, 403)
        self.assertFalse(any(method == "POST" for _, method, _ in self.events))

    async def test_test_mode_rejects_any_unregistered_engine(self):
        for field in ("llm_url", "tabby_url", "prism_url", "sc117_url", "orca_url"):
            with self.subTest(field=field), self.assertRaises(ConfigurationError):
                replace(self.config, **{field: "http://127.0.0.1:65500"})

    async def test_kobold_failed_native_load_requires_unload_and_can_retry(self):
        self.kobold.fail_load = True
        self.assertEqual((await self.chat(self.models["koboldcpp"]))[0], 500)
        self.kobold.fail_load = False
        self.assertEqual((await self.chat(self.models["koboldcpp"]))[0], 200)
        self.assertEqual(self.kobold.admin_calls, ["unload_model"])
        self.assertEqual((await self.chat(self.models["prism"]))[0], 200)
        self.assertEqual(self.kobold.admin_calls, ["unload_model", "unload_model"])

    async def test_kobold_failed_release_blocks_tabby_and_comfy(self):
        self.assertEqual((await self.chat(self.models["koboldcpp"]))[0], 200)
        self.kobold.unload_failures = 2
        self.assertEqual((await self.chat(self.models["tabbyapi"]))[0], 503)
        self.assertEqual(await self.image(), 503)
        self.assertEqual(self.tabby.loads, [])
        self.assertEqual(self.comfy.prompt_calls, [])

    async def test_released_kobold_is_not_restarted_between_other_engines(self):
        self.assertEqual((await self.chat(self.models["koboldcpp"]))[0], 200)
        self.assertEqual((await self.chat(self.models["tabbyapi"]))[0], 200)
        # Repeated native unload during the unloaded child's restart returns 502.
        self.kobold.unload_failures = 1
        self.assertEqual((await self.chat(self.models["prism"]))[0], 200)
        self.assertEqual(await self.image(), 200)
        self.assertEqual(self.kobold.admin_calls, ["unload_model"])
        # A new router request invalidates the earlier release confirmation.
        self.assertEqual((await self.chat(self.models["koboldcpp"]))[0], 200)
        self.assertEqual((await self.chat(self.models["tabbyapi"]))[0], 503)
        self.assertEqual(self.tabby.model, "inactive")

    async def test_kobold_failed_request_unloads_before_different_native_profile(self):
        self.kobold.fail_load = True
        self.assertEqual((await self.chat(self.models["koboldcpp"]))[0], 500)
        self.kobold.fail_load = False
        self.assertEqual((await self.chat("koboldcpp/b.kcpps"))[0], 200)
        self.assertEqual(self.kobold.admin_calls, ["unload_model"])
        self.assertEqual(self.kobold.selected, ["b.kcpps"])

    async def test_disconnected_waiting_request_never_switches_engine(self):
        self.prism.hold_chat = True
        works = []
        original = self.service.coordinator._dispatch_chat

        async def dispatch(work):
            works.append(work)
            await original(work)

        self.service.coordinator._dispatch_chat = dispatch
        response = await self.client.post(self.url + "/v1/chat/completions", json={
            "model": self.models["prism"], "stream": True,
        })
        await self.prism.chat_started.wait()
        async with ClientSession() as other_client:
            task = asyncio.create_task(other_client.post(self.url + "/v1/chat/completions",
                                        json={"model": self.models["tabbyapi"]}))
            await wait_until(lambda: len(works) == 2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await wait_until(lambda: works[-1].cancelled)
        self.prism.chat_release.set()
        await response.read()
        response.close()
        self.assertEqual((await self.chat(self.models["prism"]))[0], 200)
        self.assertEqual(self.tabby.loads, [])
        self.assertEqual(self.prism.operations, [("load", self.prism.model)])

    async def test_comfy_memory_confirmation_is_required_before_any_llm_load(self):
        self.comfy.invalid = True
        self.service.coordinator._comfy._config = replace(self.config, cleanup_timeout=5.2)
        self.assertEqual((await self.chat(self.models["tabbyapi"]))[0], 503)
        self.assertGreater(self.comfy.memory_reads, 0)
        self.assertEqual(self.tabby.loads, [])
        self.assertEqual(self.kobold.selected, [])
        self.assertEqual(self.prism.operations, [])

    async def test_image_in_progress_holds_all_model_requests_until_completion(self):
        self.comfy.auto_complete = False
        self.assertEqual(await self.image(), 200)
        tasks = [asyncio.create_task(self.chat(model)) for model in self.models.values()]
        await wait_until(
            lambda: self.service.coordinator.status()["waiting_chats"] == len(self.models))
        self.assertEqual(self.tabby.loads, [])
        self.assertEqual(self.prism.operations, [])
        self.assertEqual(self.kobold.selected, [])
        self.comfy.completion[self.comfy.prompt_calls[0]].set()
        self.assertEqual([result[0] for result in await asyncio.gather(*tasks)],
                         [200] * len(self.models))

    async def test_identical_native_ids_are_unambiguous_and_bounds_checked(self):
        self.tabby.available_models.append(self.prism.model)
        model = f"tabbyapi/{self.prism.model}"
        self.assertEqual((await self.chat(model))[0], 200)
        self.assertEqual(self.tabby.loads[-1]["model_name"], self.prism.model)
        self.assertEqual(self.prism.operations, [])

        async with self.client.post(self.url + "/v1/chat/completions?model=" + model,
                                    json={"model": self.models["prism"]}) as response:
            self.assertEqual(response.status, 400)
        self.service.config = replace(self.config, max_chat_body_bytes=8)
        self.assertEqual((await self.chat(self.models["prism"]))[0], 413)
        self.assertEqual(self.prism.operations, [])

    async def test_metadata_and_abort_use_active_engine_without_switching(self):
        self.assertEqual((await self.chat(self.models["prism"]))[0], 200)
        async with self.client.get(self.url + "/props") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["model"], self.prism.model)
        async with self.client.get(self.url + "/props", params={
            "model": self.models["tabbyapi"],
        }) as response:
            self.assertEqual(response.status, 503)
        self.assertEqual(self.tabby.loads, [])
        self.kobold.hold_chat = True
        response = await self.client.post(self.url + "/v1/chat/completions", json={
            "model": self.models["koboldcpp"], "stream": True,
        })
        await self.kobold.chat_started.wait()
        async with self.client.post(self.url + "/api/extra/abort") as abort:
            self.assertEqual(abort.status, 200)
        await response.read()
        response.close()
        self.assertIn("abort", self.kobold.events)

    async def test_strata_failure_blocks_other_backends_and_comfy(self):
        self.assertEqual((await self.chat(self.models["sc117"]))[0], 200)
        self.sc117.fail_action = "unload"
        self.assertEqual((await self.chat(self.models["orca"]))[0], 503)
        self.assertEqual(await self.image(), 503)
        self.assertEqual(self.orca.operations, [])
        self.assertEqual(self.comfy.prompt_calls, [])

    async def test_strata_load_failure_keeps_ownership_uncertain(self):
        self.sc117.fail_action = "load"
        self.assertEqual((await self.chat(self.models["sc117"]))[0], 503)
        self.assertEqual((await self.chat(self.models["orca"]))[0], 503)
        self.assertEqual(await self.image(), 503)
        self.assertEqual(self.orca.operations, [])

    async def test_strata_unload_ack_precedes_switch(self):
        self.assertEqual((await self.chat(self.models["sc117"]))[0], 200)
        self.sc117.confirm.clear()
        switch = asyncio.create_task(self.chat(self.models["orca"]))
        await wait_until(lambda: self.sc117.operations == ["load", "unload"])
        self.assertEqual(self.orca.operations, [])
        self.assertFalse(switch.done())
        self.sc117.confirm.set()
        self.assertEqual((await switch)[0], 200)

    async def test_strata_stream_holds_gpu_until_response_finishes(self):
        self.orca.hold_chat = True
        response = await self.client.post(self.url + "/v1/chat/completions", json={
            "model": self.models["orca"], "stream": True,
        })
        await self.orca.chat_started.wait()
        sequence = self.service.coordinator._sequence
        switch = asyncio.create_task(self.chat(self.models["sc117"]))
        await wait_until(lambda: self.service.coordinator._sequence > sequence)
        self.assertFalse(switch.done())
        self.assertEqual(self.orca.operations, ["load"])
        self.assertEqual(self.sc117.operations, [])
        self.orca.chat_release.set()
        self.assertEqual(await response.read(), b"data: synthetic-one\n\ndata: synthetic-two\n\n")
        response.close()
        self.assertEqual((await switch)[0], 200)
        self.assertEqual(self.orca.operations, ["load", "unload"])

    async def test_cold_kobold_initial_alias_loads_selected_profile_only_on_request(self):
        self.service.config = replace(self.config, kobold_default_model="a.kcpps")
        await check_backend(self.config, 1)
        self.assertEqual(self.kobold.loads, [])
        self.assertEqual((await self.chat("koboldcpp/initial_model"))[0], 200)
        self.assertEqual(self.kobold.selected, ["a.kcpps"])
        self.assertEqual((await self.chat(self.models["orca"]))[0], 200)
        self.assertEqual((await self.chat("koboldcpp/initial_model"))[0], 200)
        self.assertEqual(self.kobold.selected, ["a.kcpps", "a.kcpps"])
