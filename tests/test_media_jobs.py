from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientSession, web

from st_proxy import media_jobs
from st_proxy.config import BrokerConfig, TestEndpointRegistry
from st_proxy.media_jobs import JobError
from st_proxy.service import BrokerService
from st_proxy.workflows import WorkflowError

from .support import DynamicServer, MockComfy, MockKobold, wait_until
from .test_workflows import write_catalog

PNG = b"\x89PNG\r\n\x1a\nsynthetic image bytes"


class ImageComfy(MockComfy):
    def __init__(self, source: Path, *, helto=True):
        super().__init__()
        self.source, self.helto = source, helto
        source.mkdir(parents=True)
        (source / "image.png").write_bytes(PNG)
        self.references = {}
        self.submissions = []
        self.generic_paths = []
        self.resolve_calls = 0
        self.resolve_failure = False
        self.outside = False
        self.view_failure = False
        self.compress_view = False
        self.missing_outputs = False

    async def generic(self, request):
        self.generic_paths.append(request.path)
        if request.path == "/helto_save_image_advanced/resolve_reference":
            self.resolve_calls += 1
            token = (await request.json())["reference_token"]
            path = self.references.pop(token, None)
            if path is None or self.resolve_failure:
                return web.json_response({"error": "reference_not_found"}, status=404)
            if self.outside:
                path = self.source.parent / "outside.png"
                path.write_bytes(PNG)
            return web.json_response({"schema_version": 1, "path": str(path),
                                      "media_type": "image/png"})
        if request.path == "/view":
            if self.view_failure:
                return web.Response(status=404)
            assert request.query["filename"] == "image.png"
            response = web.Response(body=PNG, content_type="image/png")
            if self.compress_view:
                response.enable_compression()
            return response
        return await super().generic(request)

    async def prompt(self, request):
        self.submissions.append(await request.json())
        return await super().prompt(request)

    async def history(self, request):
        response = await super().history(request)
        payload = json.loads(response.body)
        prompt_id = request.match_info["prompt_id"]
        entry = payload.get(prompt_id)
        if entry and entry["status"]["status_str"] == "success":
            if self.helto:
                token = "reference-" + prompt_id
                self.references[token] = self.source / "image.png"
                images = {"helto_saved_images": [{"schema_version": 1,
                           "reference_token": token, "media_type": "image/png"}]}
            else:
                images = {"images": [{"filename": "image.png", "subfolder": "",
                                      "type": "output"}]}
            entry["outputs"] = {} if self.missing_outputs else {"3": images}
        return web.json_response(payload, status=response.status)


class MediaJobsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="st-proxy-mcp-test-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.catalog_dir = self.directory / "workflows"
        write_catalog(self.catalog_dir)
        self.comfy = ImageComfy(self.catalog_dir / "source")
        self.llm = MockKobold()
        self.comfy_server = DynamicServer(self.comfy.app())
        self.llm_server = DynamicServer(self.llm.app())
        await self.comfy_server.start()
        self.addAsyncCleanup(self.comfy_server.close)
        await self.llm_server.start()
        self.addAsyncCleanup(self.llm_server.close)
        registry = TestEndpointRegistry()
        registry.approve_disposable(self.comfy_server.url)
        registry.approve_disposable(self.llm_server.url)
        self.config = BrokerConfig.for_test(
            llm_url=self.llm_server.url, comfy_url=self.comfy_server.url, registry=registry,
            mcp_port=0, mcp_token="synthetic-mcp-token", mcp_workflows_dir=str(self.catalog_dir),
            mcp_output_dir=str(self.directory / "results"),
            poll_interval=0.005,
        )
        self.service = BrokerService(self.config)
        self.addAsyncCleanup(self.service.stop)
        await self.service.start()
        self.jobs = self.service.media_jobs
        self.client = ClientSession()
        self.addAsyncCleanup(self.client.close)

    async def generate(self, job_id="job1", **parameters):
        return await self.jobs.generate(job_id, "example", {"prompt": "SYNTHETIC_SECRET",
                                                           **parameters})

    async def test_helto_result_privacy_and_request_driven_restore(self):
        with self.assertLogs("st_proxy", "INFO") as logs:
            result = await self.generate()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(Path(result["images"][0]["path"]).read_bytes(), PNG)
        self.assertTrue(
            Path(result["images"][0]["path"]).is_relative_to(self.directory / "results")
        )
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result) + "".join(logs.output))
        graph = self.comfy.submissions[0]["prompt"]
        self.assertEqual(graph["1"]["inputs"]["positive_prompt"], "SYNTHETIC_SECRET")
        original = json.loads((self.catalog_dir / "workflow.json").read_text())
        self.assertEqual(graph["1"]["inputs"]["negative_prompt"],
                         original["1"]["inputs"]["negative_prompt"])
        self.assertNotIn("/aio_image_generate/privacy/encrypt", self.comfy.generic_paths)
        self.assertTrue(graph["3"]["inputs"]["reference_only"])
        self.assertTrue(graph["3"]["inputs"]["privacy_mode"])
        self.assertFalse(json.loads((self.catalog_dir / "workflow.json").read_text())[
            "3"]["inputs"]["reference_only"])
        self.assertEqual(self.llm.admin_calls, ["unload_model"])
        self.assertEqual(self.comfy.free_calls, 0)
        self.assertEqual(self.service.coordinator.status()["gpu_owner"], "comfy")
        self.assertEqual(await self.jobs.get_job("job1"), result)
        self.assertEqual(self.comfy.free_calls, 0)
        # Existing suites cover the real five-second cleanup delay.
        real_sleep = asyncio.sleep

        async def skip_fixed_wait(delay):
            await real_sleep(0 if delay == 5.0 else delay)

        with patch("st_proxy.comfy.asyncio.sleep", side_effect=skip_fixed_wait):
            async with self.client.post(
                f"http://127.0.0.1:{self.service.chat_port}/api/v1/generate", json={},
            ) as response:
                self.assertEqual(response.status, 200)
        self.assertEqual(self.comfy.free_calls, 1)
        self.assertEqual(self.llm.admin_calls, ["unload_model", "initial_model"])

    async def test_bad_parameters_never_reach_gpu(self):
        with self.assertRaises(WorkflowError):
            await self.generate(seed=True)
        self.assertEqual(self.comfy.prompt_calls, [])
        self.assertEqual(self.llm.admin_calls, [])

    async def test_disconnect_and_retries_keep_one_job(self):
        self.comfy.auto_complete = False
        waiting = asyncio.create_task(self.generate())
        await wait_until(lambda: "job1" in self.jobs.jobs
                         and self.jobs.jobs["job1"].status == "generating")
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        self.assertEqual((await self.jobs.get_job("job1", False))["status"], "generating")
        retry = asyncio.create_task(self.generate())
        with self.assertRaises(JobError):
            await self.generate(seed=3)
        self.comfy.completion[self.comfy.prompt_calls[0]].set()
        result = await retry
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(self.comfy.prompt_calls), 1)
        self.assertEqual(self.comfy.resolve_calls, 1)
        self.assertEqual(await self.generate(), result)

    async def test_unknown_job_does_not_generate(self):
        with self.assertRaises(JobError):
            await self.jobs.get_job("missing")
        self.assertFalse(self.comfy.prompt_calls)

    async def test_capacity_is_bounded_and_shutdown_releases_waiter(self):
        self.jobs.config = replace(self.config, max_queued_images=1)
        self.comfy.auto_complete = False
        waiting = asyncio.create_task(self.generate())
        await wait_until(lambda: "job1" in self.jobs.jobs
                         and self.jobs.jobs["job1"].status == "generating")
        with self.assertRaisesRegex(JobError, "capacity"):
            await self.generate("too-many")
        await self.service.stop()
        result = await waiting
        self.assertEqual(result["error"]["category"], "shutdown")
        self.assertEqual(len(self.comfy.prompt_calls), 1)
        self.assertEqual(self.llm.admin_calls, ["unload_model"])

    async def test_copy_failure_cleans_staging_and_keeps_source(self):
        with patch("st_proxy.media_jobs._copy_local", side_effect=OSError("synthetic disk error")):
            result = await self.generate()
        self.assertEqual(result["error"]["category"], "result")
        self.assertTrue((self.comfy.source / "image.png").exists())
        self.assertEqual(list((self.directory / "results").iterdir()), [])

    async def test_shutdown_during_copy_leaves_no_partial_result(self):
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        copy_local = media_jobs._copy_local

        def held_copy(*args):
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise TimeoutError("synthetic copy was not released")
            copy_local(*args)

        with patch("st_proxy.media_jobs._copy_local", side_effect=held_copy):
            waiting = asyncio.create_task(self.generate())
            stopping = None
            try:
                await asyncio.wait_for(started.wait(), 3)
                stopping = asyncio.create_task(self.service.stop())
                await wait_until(lambda: self.jobs.jobs["job1"].task.cancelling() > 0)
            finally:
                release.set()
                if stopping is not None:
                    await stopping
                result = await waiting
        self.assertEqual(result["error"]["category"], "shutdown")
        self.assertEqual(list((self.directory / "results").iterdir()), [])
        self.assertEqual((self.comfy.source / "image.png").read_bytes(), PNG)

    async def test_reference_resolution_continues_without_a_waiter(self):
        self.comfy.auto_complete = False
        waiting = asyncio.create_task(self.generate())
        await wait_until(lambda: bool(self.comfy.prompt_calls))
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        self.comfy.completion[self.comfy.prompt_calls[0]].set()
        await wait_until(lambda: self.jobs.jobs["job1"].status == "succeeded")
        self.assertEqual(self.comfy.resolve_calls, 1)
        self.assertEqual((await self.jobs.get_job("job1"))["status"], "succeeded")

    async def test_active_llm_stream_finishes_before_mcp_handoff(self):
        real_sleep = asyncio.sleep

        async def skip_fixed_wait(delay):
            await real_sleep(0 if delay == 5.0 else delay)

        self.llm.hold_chat = True
        with patch("st_proxy.comfy.asyncio.sleep", side_effect=skip_fixed_wait):
            response = await self.client.post(
                f"http://127.0.0.1:{self.service.chat_port}/api/extra/generate/stream", json={},
            )
        try:
            await self.llm.chat_started.wait()
            waiting = asyncio.create_task(self.generate())
            await wait_until(lambda: self.service.coordinator.status()["state"] == "draining_llm")
            self.assertNotIn("unload_model", self.llm.admin_calls)
            self.assertFalse(self.comfy.prompt_calls)
            self.llm.chat_release.set()
            await response.read()
            self.assertEqual((await waiting)["status"], "succeeded")
        finally:
            self.llm.chat_release.set()
            response.close()

    async def test_cleanup_failure_still_prevents_next_llm_load(self):
        self.assertEqual((await self.generate())["status"], "succeeded")
        self.comfy.cleanup_failures = 1
        async with self.client.post(
            f"http://127.0.0.1:{self.service.chat_port}/api/v1/generate", json={},
        ) as response:
            self.assertEqual(response.status, 503)
        self.assertEqual(self.llm.admin_calls, ["unload_model"])

    async def test_generation_failure_and_timeout_are_terminal_without_restore(self):
        self.comfy.fail_next_job = True
        self.assertEqual((await self.generate())["error"]["category"], "generation")
        self.comfy.auto_complete = False
        self.service.coordinator._comfy._config = replace(self.config, image_timeout=0.02)
        self.assertEqual((await self.generate("timeout"))["status"], "failed")
        self.assertEqual(self.comfy.interrupt_calls, 1)
        self.assertEqual(self.llm.admin_calls, ["unload_model"])

    async def test_result_failures_do_not_regenerate_or_leave_partial_files(self):
        for flag in ("resolve_failure", "outside", "missing_outputs"):
            with self.subTest(flag=flag):
                setattr(self.comfy, flag, True)
                result = await self.generate(flag)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["images"], [])
                self.assertEqual(await self.generate(flag), result)
                setattr(self.comfy, flag, False)
        self.assertEqual(len(self.comfy.prompt_calls), 3)
        self.assertEqual(list((self.directory / "results").iterdir()), [])

    async def test_plain_output_uses_view(self):
        from st_proxy.workflows import WorkflowCatalog
        write_catalog(self.catalog_dir, helto=False)
        self.jobs.catalog = WorkflowCatalog(str(self.catalog_dir), 100000)
        self.comfy.helto = False
        self.comfy.compress_view = True
        result = await self.generate()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(Path(result["images"][0]["path"]).read_bytes(), PNG)
        self.comfy.view_failure = True
        self.assertEqual((await self.generate("bad-view"))["status"], "failed")

    async def test_shutdown_finishes_waiters_without_restore(self):
        self.comfy.auto_complete = False
        waiting = asyncio.create_task(self.generate())
        await wait_until(lambda: self.jobs.jobs["job1"].status == "generating"
                         if "job1" in self.jobs.jobs else False)
        await self.service.stop()
        result = await asyncio.wait_for(waiting, 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["category"], "shutdown")
        self.assertEqual(self.llm.admin_calls, ["unload_model"])
        self.assertEqual(self.comfy.interrupt_calls, 1)

    async def test_http_acceptance_and_mcp_share_fifo(self):
        self.comfy.auto_complete = False
        async with self.client.post(f"http://127.0.0.1:{self.service.image_port}/prompt",
                                    json={"prompt": {}}) as response:
            self.assertEqual(response.status, 200)
        waiting = asyncio.create_task(self.generate())
        await wait_until(lambda: self.service.coordinator.status()["waiting_images"] == 1)
        self.assertEqual(len(self.comfy.prompt_calls), 1)
        self.comfy.completion[self.comfy.prompt_calls[0]].set()
        await wait_until(lambda: len(self.comfy.prompt_calls) == 2)
        self.assertFalse(waiting.done())
        self.comfy.completion[self.comfy.prompt_calls[1]].set()
        self.assertEqual((await waiting)["status"], "succeeded")

    async def test_batch_is_ordered_private_and_idempotent(self):
        items = [{"prompt": "SECRET_FOREST"}, {"prompt": "SECRET_LAKE", "seed": 2}]
        with self.assertLogs("st_proxy", "INFO") as logs:
            result = await self.jobs.generate_batch("batch", "example", items)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual([item["index"] for item in result["items"]], [0, 1])
        self.assertEqual([s["prompt"]["1"]["inputs"]["positive_prompt"]
                          for s in self.comfy.submissions], [p["prompt"] for p in items])
        paths = [item["images"][0]["path"] for item in result["items"]]
        self.assertEqual(len(set(paths)), 2)
        self.assertTrue(all(Path(path).read_bytes() == PNG for path in paths))
        self.assertNotIn("SECRET_", json.dumps(result) + "".join(logs.output))
        self.assertEqual(self.llm.admin_calls, ["unload_model"])
        self.assertEqual(self.comfy.free_calls, 0)
        self.assertEqual(await self.jobs.generate_batch("batch", "example", items), result)
        self.assertEqual(await self.jobs.get_job("batch"), result)
        for changed in (list(reversed(items)), items[:1]):
            with self.assertRaisesRegex(JobError, "another request"):
                await self.jobs.generate_batch("batch", "example", changed)
        with self.assertRaisesRegex(JobError, "another request"):
            await self.jobs.generate("batch", "example", items[0])
        self.assertEqual(len(self.comfy.prompt_calls), 2)

    async def test_batch_validates_every_item_before_touching_gpu(self):
        for items in ([], [{"prompt": "valid"}, {"prompt": "bad", "seed": True}],
                      [{"prompt": "valid"}, {"unknown": "bad"}],
                      [{"prompt": "x"}] * (self.config.max_queued_images + 1)):
            with self.subTest(items=items), self.assertRaises((JobError, WorkflowError)):
                await self.jobs.generate_batch("invalid", "example", items)
        self.assertFalse(self.comfy.prompt_calls)
        self.assertFalse(self.llm.admin_calls)
        self.assertFalse(self.jobs.jobs)

    async def test_batch_saves_early_and_blocks_later_chat_between_images(self):
        self.comfy.auto_complete = False
        self.llm.hold_chat = True
        real_sleep = asyncio.sleep

        async def skip_fixed_wait(delay):
            await real_sleep(0 if delay == 5.0 else delay)

        with patch("st_proxy.comfy.asyncio.sleep", side_effect=skip_fixed_wait):
            # A chat already ahead of the batch must finish first.
            first = await self.client.post(
                f"http://127.0.0.1:{self.service.chat_port}/api/extra/generate/stream", json={},
            )
            free_before_batch = self.comfy.free_calls
            waiting = asyncio.create_task(self.jobs.generate_batch("batch", "example", [
                {"prompt": "one"}, {"prompt": "two"}, {"prompt": "three"},
            ]))
            await wait_until(lambda: self.service.coordinator.status()["state"] == "draining_llm")
            self.assertFalse(self.comfy.prompt_calls)
            later = asyncio.create_task(self.client.post(
                f"http://127.0.0.1:{self.service.chat_port}/api/v1/generate", json={},
            ))
            await wait_until(lambda: self.service.coordinator.status()["waiting_chats"] == 1)
            self.llm.chat_release.set()
            await first.read()
            first.close()
            for index in range(3):
                await wait_until(lambda index=index: len(self.comfy.prompt_calls) == index + 1)
                self.assertFalse(later.done())
                self.assertEqual(self.llm.admin_calls, ["unload_model"])
                self.assertEqual(self.comfy.free_calls, free_before_batch)
                self.comfy.completion[self.comfy.prompt_calls[index]].set()
                await wait_until(lambda index=index: self.comfy.resolve_calls == index + 1)
            result = await waiting
            self.assertEqual(result["status"], "succeeded")
            response = await later
            await response.read()
            response.close()
        self.assertEqual(self.llm.admin_calls, ["unload_model", "initial_model"])
        self.assertEqual(self.comfy.free_calls, free_before_batch + 1)

    async def test_batch_disconnect_keeps_work_and_partial_results(self):
        self.comfy.auto_complete = False
        self.comfy.fail_next_job = True
        self.jobs.config = replace(self.config, max_queued_images=2)
        items = [{"prompt": "fails"}, {"prompt": "works"}]
        waiting = asyncio.create_task(self.jobs.generate_batch("batch", "example", items))
        await wait_until(lambda: bool(self.comfy.prompt_calls))
        with self.assertRaisesRegex(JobError, "capacity"):
            await self.generate("over-capacity")
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        for index in range(2):
            await wait_until(lambda index=index: len(self.comfy.prompt_calls) == index + 1)
            self.comfy.completion[self.comfy.prompt_calls[index]].set()
        result = await self.jobs.get_job("batch")
        self.assertEqual(result["status"], "partial")
        self.assertEqual([item["status"] for item in result["items"]], ["failed", "succeeded"])
        self.assertEqual(Path(result["items"][1]["images"][0]["path"]).read_bytes(), PNG)
        self.assertEqual(await self.jobs.generate_batch("batch", "example", items), result)
        self.assertEqual(len(self.comfy.prompt_calls), 2)
        self.assertEqual(self.llm.admin_calls, ["unload_model"])

    async def test_batch_admission_is_atomic_for_count_and_byte_limits(self):
        workflow = self.jobs.catalog.get("example")
        body_size = len(json.dumps({"prompt": workflow.build(
            workflow.normalize({"prompt": "one"}),
        )}, allow_nan=False).encode())
        for overrides in ({"max_queued_images": 1},
                          {"max_queued_workflow_bytes": body_size,
                           "max_workflow_body_bytes": body_size},
                          {"max_workflow_body_bytes": 1}):
            with self.subTest(overrides=overrides):
                # Keep the MCP pre-admission limits unchanged to exercise the shared queue.
                coordinator = self.service.coordinator
                coordinator._config = replace(self.config, **overrides)
                result = await self.jobs.generate_batch(str(len(self.jobs.jobs)), "example", [
                    {"prompt": "one"}, {"prompt": "two"},
                ])
                self.assertEqual(result["status"], "failed")
                self.assertTrue(all(i["status"] == "failed" for i in result["items"]))
                self.assertEqual(coordinator.status()["waiting_images"], 0)
                self.assertEqual(coordinator.status()["queued_workflow_bytes"], 0)
        self.assertFalse(self.comfy.prompt_calls)
        self.assertFalse(self.llm.admin_calls)

    async def test_shutdown_batch_keeps_saved_images_and_cancels_remaining(self):
        self.comfy.auto_complete = False
        waiting = asyncio.create_task(self.jobs.generate_batch("batch", "example", [
            {"prompt": "one"}, {"prompt": "two"}, {"prompt": "three"},
        ]))
        await wait_until(lambda: len(self.comfy.prompt_calls) == 1)
        self.comfy.completion[self.comfy.prompt_calls[0]].set()
        await wait_until(lambda: self.jobs.jobs["batch"].items[0].status == "succeeded")
        await wait_until(lambda: len(self.comfy.prompt_calls) == 2)
        await self.service.stop()
        result = await asyncio.wait_for(waiting, 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["category"], "shutdown")
        self.assertEqual([item["status"] for item in result["items"]],
                         ["succeeded", "failed", "failed"])
        self.assertEqual(Path(result["items"][0]["images"][0]["path"]).read_bytes(), PNG)
        self.assertEqual(len(self.comfy.prompt_calls), 2)
        self.assertEqual(self.llm.admin_calls, ["unload_model"])
