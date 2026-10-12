from __future__ import annotations

import asyncio
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from st_proxy.workflows import MAX_SEED, Workflow, WorkflowCatalog, WorkflowError

from . import test_reference_images as refs_fixture
from .support import wait_until
from .test_workflows import write_catalog

ROOT = Path(__file__).resolve().parents[1] / "workflows"


def random_seed_manifest(directory):
    path = directory / "example.manifest.json"
    manifest = json.loads(path.read_text())
    seed = manifest["parameters"]["seed"]
    seed.pop("default")
    seed["random_default"] = True
    path.write_text(json.dumps(manifest))
    return manifest


@pytest.mark.parametrize("workflow_id", ["image_generation", "qwen_scene", "image_upscale"])
def test_shipped_seed_schema_defaults_and_fixed_graphs(workflow_id):
    workflow = WorkflowCatalog(str(ROOT), 100000).get(workflow_id)
    original = copy.deepcopy(workflow.graph)
    parameters = {"prompt": "test"} if "prompt" in workflow.parameters else {}
    normalized = workflow.normalize(parameters)
    assert "seed" not in normalized
    schema = workflow.describe()["parameters"]["properties"]["seed"]
    assert schema["type"] == "integer"
    assert schema["minimum"] == 0 and schema["maximum"] == MAX_SEED
    assert "default" not in schema and "random_default" not in schema
    with patch("st_proxy.workflows.secrets.randbelow", side_effect=[0, MAX_SEED]) as rng:
        for expected in (0, MAX_SEED):
            values = dict(normalized)
            workflow.resolve_seed(values)
            assert values["seed"] == expected
            rng.assert_called_with(MAX_SEED + 1)
    references = ([{"root": "/synthetic", "subfolder": "refs", "filename": "ref.png"}]
                  if workflow.references else [])
    values = workflow.normalize({**parameters, "seed": 0})
    with patch("st_proxy.workflows.secrets.randbelow") as rng:
        workflow.resolve_seed(values)
        graph = workflow.build(values, references)
        rng.assert_not_called()
    assert graph["1"]["inputs"]["seed"] == 0
    values["seed"] = MAX_SEED
    graph["1"]["inputs"]["seed"] = MAX_SEED
    assert workflow.build(values, references) == graph
    assert workflow.graph == original
    assert workflow.graph == json.loads((ROOT / workflow.manifest["workflow"]).read_text())


@pytest.mark.parametrize("seed", [-1, MAX_SEED + 1, True, False, None, 1.0, "1", [], {}])
def test_invalid_seed_is_not_coerced(tmp_path, seed):
    write_catalog(tmp_path)
    manifest = random_seed_manifest(tmp_path)
    workflow = Workflow.load(manifest, tmp_path, 100000)
    with pytest.raises(WorkflowError, match="seed"):
        workflow.normalize({"prompt": "test", "seed": seed})


@pytest.mark.parametrize("change", [
    {"random_default": "true"}, {"required": True}, {"private": True}, {"default": 0},
    {"type": "number"}, {"minimum": -1}, {"minimum": 0.0}, {"maximum": MAX_SEED + 1},
    {"maximum": None}, {"enum": [0, 1]}, {"multipleOf": 2},
])
def test_random_seed_configuration_preserves_strict_contracts(tmp_path, change):
    write_catalog(tmp_path)
    manifest = random_seed_manifest(tmp_path)
    manifest["parameters"]["seed"].update(change)
    with pytest.raises(WorkflowError):
        Workflow.load(manifest, tmp_path, 100000)


def test_random_defaults_do_not_relax_other_optional_parameters(tmp_path):
    write_catalog(tmp_path)
    manifest = random_seed_manifest(tmp_path)
    manifest["parameters"]["other"] = manifest["parameters"].pop("seed")
    with pytest.raises(WorkflowError, match="bounded seed"):
        Workflow.load(manifest, tmp_path, 100000)
    manifest["parameters"]["seed"] = manifest["parameters"].pop("other")
    manifest["parameters"]["seed"]["random_default"] = False
    with pytest.raises(WorkflowError, match="need a default"):
        Workflow.load(manifest, tmp_path, 100000)


class SeedMCPTests(unittest.IsolatedAsyncioTestCase):
    sdk_client = refs_fixture.ReferenceJobsTests.sdk_client
    url = refs_fixture.ReferenceJobsTests.url

    async def asyncSetUp(self):
        await refs_fixture.ReferenceJobsTests.asyncSetUp(self)
        random_seed_manifest(self.catalog_dir)
        self.jobs.catalog = WorkflowCatalog(str(self.catalog_dir), 100000)

    def submitted_seeds(self):
        return [body["prompt"]["1"]["inputs"]["seed"] for body in self.comfy.submissions]

    async def test_single_random_seed_survives_disconnect_and_fixed_seed_is_reusable(self):
        self.comfy.auto_complete = False
        request = {"job_id": "random", "workflow_id": "example",
                   "parameters": {"prompt": "test"}}
        with patch("st_proxy.workflows.secrets.randbelow", side_effect=[101, 202]) as rng:
            async with self.sdk_client() as client:
                call = asyncio.create_task(client.call_tool("generate", request))
                await wait_until(lambda: bool(self.comfy.prompt_calls))
                self.assertEqual(self.submitted_seeds(), [101])
                call.cancel()
                await asyncio.gather(call, return_exceptions=True)
            self.comfy.completion[self.comfy.prompt_calls[0]].set()
            self.comfy.auto_complete = True
            async with self.sdk_client() as client:
                recovered = await client.call_tool("get_job", {"job_id": "random"})
                self.assertFalse(recovered.is_error, recovered)
                self.assertEqual(recovered.structured_content["parameters"]["seed"], 101)
                replay = await client.call_tool("generate", request)
                self.assertEqual(replay.structured_content, recovered.structured_content)
                self.assertEqual(rng.call_count, 1)
                fresh = await client.call_tool("generate", {**request, "job_id": "fresh"})
                self.assertFalse(fresh.is_error, fresh)
                self.assertEqual(fresh.structured_content["parameters"]["seed"], 202)
                for index, seed in enumerate((0, MAX_SEED, 101, 101)):
                    result = await client.call_tool("generate", {
                        **request, "job_id": f"fixed-{index}",
                        "parameters": {"prompt": "test", "seed": seed},
                    })
                    self.assertFalse(result.is_error, result)
                    self.assertEqual(result.structured_content["parameters"]["seed"], seed)
                conflict = await client.call_tool("generate", {
                    **request, "parameters": {"prompt": "test", "seed": 101},
                })
                self.assertTrue(conflict.is_error)
                self.assertEqual(conflict.structured_content["error"]["category"], "conflict")
            self.assertEqual(rng.call_count, 2)
        self.assertEqual(self.submitted_seeds(), [101, 202, 0, MAX_SEED, 101, 101])
        self.assertEqual(self.llm.admin_calls, ["unload_model"])

    async def test_mixed_batch_seeds_references_metadata_and_retries(self):
        reference = await self.jobs.register_image("example", str(self.source))
        items = [
            {"prompt": "one"},
            {"parameters": {"prompt": "two", "seed": 0},
             "references": [reference["image_id"]]},
            {"parameters": {"prompt": "three"}, "references": [reference["image_id"]]},
            {"prompt": "four", "seed": 7}, {"prompt": "five", "seed": 7},
        ]
        request = {"job_id": "mixed", "workflow_id": "example", "items": items}
        with patch("st_proxy.workflows.secrets.randbelow", side_effect=[11, 22, 33, 44]) as rng:
            async with self.sdk_client() as client:
                result = await client.call_tool("generate_batch", request)
                self.assertFalse(result.is_error, result)
                self.assertEqual(result.structured_content["status"], "succeeded")
                self.assertEqual([item["parameters"]["seed"]
                                  for item in result.structured_content["items"]],
                                 [11, 0, 22, 7, 7])
                recovered = await client.call_tool("get_job", {"job_id": "mixed"})
                replay = await client.call_tool("generate_batch", request)
                self.assertEqual(recovered.structured_content, result.structured_content)
                self.assertEqual(replay.structured_content, result.structured_content)
                self.assertEqual(rng.call_count, 2)
                changed = copy.deepcopy(items)
                changed[1]["parameters"]["seed"] = 1
                conflict = await client.call_tool("generate_batch", {**request, "items": changed})
                self.assertTrue(conflict.is_error)
                self.assertEqual(conflict.structured_content["error"]["category"], "conflict")
                fresh = await client.call_tool("generate_batch", {**request, "job_id": "fresh"})
                self.assertFalse(fresh.is_error, fresh)
                self.assertEqual([item["parameters"]["seed"]
                                  for item in fresh.structured_content["items"]],
                                 [33, 0, 44, 7, 7])
                self.assertEqual(rng.call_count, 4)
        self.assertEqual(self.submitted_seeds(), [11, 0, 22, 7, 7, 33, 0, 44, 7, 7])
        self.assertIn("image 1", self.comfy.submissions[1]["prompt"]["1"]["inputs"])
        self.assertIn("image 1", self.comfy.submissions[2]["prompt"]["1"]["inputs"])
        self.assertEqual(self.llm.admin_calls, ["unload_model"])

    async def test_bad_late_seed_is_rejected_before_rng_jobs_or_gpu(self):
        with patch("st_proxy.workflows.secrets.randbelow") as rng:
            async with self.sdk_client() as client:
                for tool, args in (
                    ("generate", {"parameters": {"prompt": "test", "seed": True}}),
                    ("generate_batch", {"items": [
                        {"prompt": "one"}, {"parameters": {"prompt": "two", "seed": -1}},
                    ]}),
                ):
                    response = await client.call_tool(tool, {
                        "job_id": "bad", "workflow_id": "example", **args,
                    })
                    self.assertTrue(response.is_error)
                    self.assertEqual(response.structured_content["error"]["category"], "validation")
            rng.assert_not_called()
        self.assertFalse(self.jobs.jobs)
        self.assertFalse(self.comfy.submissions)
        self.assertFalse(self.llm.admin_calls)
