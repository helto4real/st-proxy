from __future__ import annotations

import asyncio
import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from st_proxy.media_jobs import JobError
from st_proxy.workflows import WorkflowCatalog, WorkflowError

from . import test_reference_images as fixtures
from .support import wait_until
from .test_workflow_modes import write_modes


class ModeJobsTests(unittest.IsolatedAsyncioTestCase):
    sdk_client = fixtures.ReferenceJobsTests.sdk_client
    url = fixtures.ReferenceJobsTests.url
    register = fixtures.ReferenceJobsTests.register

    async def asyncSetUp(self):
        await fixtures.ReferenceJobsTests.asyncSetUp(self)
        write_modes(self.catalog_dir)
        self.jobs.catalog = WorkflowCatalog(str(self.catalog_dir), 100000)

    async def test_sdk_discovers_modes_and_default_retries_are_identical(self):
        async with self.sdk_client() as client:
            description = await client.call_tool("describe_workflow", {"workflow_id": "example"})
            schema = description.structured_content["parameters"]["properties"]["mode"]
            self.assertEqual(schema["enum"], ["turbo", "quality"])
            self.assertEqual(schema["default"], "turbo")
            self.assertIn("Synthetic quality", schema["description"])
            self.assertFalse(self.llm.admin_calls)
            request = {"job_id": "default", "workflow_id": "example",
                       "parameters": {"prompt": "x"}}
            original = await client.call_tool("generate", request)
            self.assertFalse(original.is_error, original)
            self.assertEqual(original.structured_content["parameters"]["mode"], "turbo")
            request["parameters"]["mode"] = "turbo"
            retry = await client.call_tool("generate", request)
            self.assertEqual(retry.structured_content, original.structured_content)
            request["parameters"]["mode"] = "quality"
            conflict = await client.call_tool("generate", request)
            self.assertEqual(conflict.structured_content["error"]["category"], "conflict")
            request["job_id"] = "quality"
            quality = await client.call_tool("generate", request)
            self.assertFalse(quality.is_error, quality)
            self.assertEqual(quality.structured_content["parameters"]["mode"], "quality")
            self.assertEqual(len(self.comfy.submissions), 2)
            self.assertNotIn("sigmas", self.comfy.submissions[1]["prompt"]["1"]["inputs"])
            self.assertEqual(self.llm.admin_calls, ["unload_model"])

    async def test_mixed_batch_reference_order_partial_results_and_identity(self):
        first = await self.register()
        second_path = self.inputs / "blue.png"
        second_path.write_bytes(fixtures.image_bytes("blue"))
        second = await self.register(second_path)
        ids = [first["image_id"], second["image_id"]]
        items = [{"parameters": {"prompt": "one", "mode": "quality"}, "references": ids},
                 {"prompt": "two", "mode": "turbo"},
                 {"parameters": {"prompt": "three", "mode": "quality"},
                  "references": list(reversed(ids))}]
        self.comfy.fail_next_job = True
        async with self.sdk_client() as client:
            result = await client.call_tool("generate_batch", {
                "job_id": "mixed", "workflow_id": "example", "items": items,
            })
        payload = result.structured_content
        self.assertEqual(payload["status"], "partial")
        self.assertEqual([i["status"] for i in payload["items"]],
                         ["failed", "succeeded", "succeeded"])
        self.assertEqual([i["parameters"]["mode"] for i in payload["items"]],
                         ["quality", "turbo", "quality"])
        graphs = [submission["prompt"] for submission in self.comfy.submissions]
        self.assertEqual([g["1"]["inputs"]["positive_prompt"] for g in graphs],
                         ["one", "two", "three"])
        self.assertEqual([g["1"]["inputs"]["steps"] for g in graphs], [20, 8, 20])
        for graph, expected in ((graphs[0], [first, second]), (graphs[2], [second, first])):
            self.assertNotIn("sigmas", graph["1"]["inputs"])
            filenames = [graph[graph["1"]["inputs"][f"image {i}"][0]]["inputs"]["filename"]
                         for i in (1, 2)]
            self.assertEqual(filenames, [Path(r["preview_path"]).name for r in expected])
        self.assertEqual(graphs[1]["1"]["inputs"]["sigmas"], ["13", 0])
        self.assertNotIn("image 1", graphs[1]["1"]["inputs"])
        self.assertEqual(self.llm.admin_calls, ["unload_model"])
        self.assertEqual(self.comfy.free_calls, 0)
        items[1].pop("mode")  # Default and explicit turbo have the same identity.
        self.assertEqual(await self.jobs.generate_batch("mixed", "example", items), payload)
        changed = copy.deepcopy(items)
        changed[0]["parameters"]["mode"] = "turbo"
        with self.assertRaisesRegex(JobError, "another request"):
            await self.jobs.generate_batch("mixed", "example", changed)
        self.assertEqual(len(self.comfy.submissions), 3)

    async def test_invalid_mode_rejects_whole_batch_before_admission(self):
        for invalid in ({"prompt": "x", "mode": "unknown"},
                        {"parameters": {"prompt": "x", "mode": False}, "references": []}):
            with self.assertRaises(WorkflowError):
                await self.jobs.generate_batch("bad", "example", [{"prompt": "valid"}, invalid])
        self.assertFalse(self.jobs.jobs)
        self.assertFalse(self.comfy.submissions)
        self.assertFalse(self.llm.admin_calls)

    async def test_mixed_modes_keep_gpu_until_batch_finishes_then_restore_for_chat(self):
        self.comfy.auto_complete = False
        real_sleep = asyncio.sleep

        async def skip_fixed_wait(delay):
            await real_sleep(0 if delay == 5.0 else delay)

        with patch("st_proxy.comfy.asyncio.sleep", side_effect=skip_fixed_wait):
            waiting = asyncio.create_task(self.jobs.generate_batch("ordered", "example", [
                {"prompt": "one", "mode": "quality"}, {"prompt": "two", "mode": "turbo"},
            ]))
            await wait_until(lambda: len(self.comfy.prompt_calls) == 1)
            later = asyncio.create_task(self.client.post(
                f"http://127.0.0.1:{self.service.chat_port}/api/v1/generate", json={},
            ))
            await wait_until(lambda: self.service.coordinator.status()["waiting_chats"] == 1)
            self.comfy.completion[self.comfy.prompt_calls[0]].set()
            await wait_until(lambda: len(self.comfy.prompt_calls) == 2)
            self.assertFalse(later.done())
            self.assertEqual(self.comfy.free_calls, 0)
            self.assertEqual(self.llm.admin_calls, ["unload_model"])
            self.comfy.completion[self.comfy.prompt_calls[1]].set()
            self.assertEqual((await waiting)["status"], "succeeded")
            response = await later
            await response.read()
            response.close()
        self.assertEqual(self.comfy.free_calls, 1)
        self.assertEqual(self.llm.admin_calls, ["unload_model", "initial_model"])
