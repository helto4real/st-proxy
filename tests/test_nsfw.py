from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

import pytest

from st_proxy.workflows import Workflow, WorkflowCatalog, WorkflowError

from . import test_mcp_server as transport
from . import test_media_jobs as fixtures
from .test_workflow_modes import write_modes
from .test_workflows import write_catalog

ROOT = Path(__file__).resolve().parents[1] / "workflows"


def write_nsfw_catalog(directory):
    manifest, modes = write_modes(directory)
    manifest["nsfw_loras"] = {"node": "11", "inputs": ["lora_1"]}
    del modes["modes"]["quality"]["set"]["11"]
    (directory / "modes.yaml").write_text(json.dumps(modes))
    (directory / "example.manifest.json").write_text(json.dumps(manifest))
    return manifest


@pytest.mark.parametrize("workflow_id", ["image_generation", "qwen_scene"])
@pytest.mark.parametrize("mode", ["standard", "quick draft"])
def test_shipped_nsfw_only_changes_selected_lora_activation(workflow_id, mode):
    workflow = WorkflowCatalog(str(ROOT), 100000).get(workflow_id)
    original = copy.deepcopy(workflow.graph)
    references = ([{"root": "/synthetic", "subfolder": "references", "filename": "ref.png"}]
                  if workflow.references else [])
    parameters = {"prompt": "synthetic test", "mode": mode,
                  "seed": original["1"]["inputs"]["seed"]}
    off = workflow.build(workflow.normalize(parameters), references)
    assert workflow.normalize(parameters) == workflow.normalize({**parameters, "nsfw": False})
    assert off["11"]["inputs"]["lora_2"] == {
        "on": False, "lora": "qwen-image-21/xxx/NSFW Qwen Lora.safetensors", "strength": 1,
    }
    expected = copy.deepcopy(off)
    expected["11"]["inputs"]["lora_2"]["on"] = True
    assert workflow.build(workflow.normalize({**parameters, "nsfw": True}), references) == expected
    assert workflow.build(workflow.normalize(parameters), references) == off
    assert workflow.graph == original
    assert workflow.describe()["parameters"]["properties"]["nsfw"]["default"] is False


def test_nsfw_default_overrides_export_activation_and_preserves_other_loras(tmp_path):
    graph, _ = write_catalog(tmp_path)
    manifest = write_nsfw_catalog(tmp_path)
    inputs = graph["11"]["inputs"]
    inputs["lora_1"]["on"] = True
    inputs["lora_1"]["strength"] = 0.7
    inputs["lora_2"] = {"on": False, "lora": "second.safetensors", "strength": 0.4}
    inputs["lora_3"] = {"on": True, "lora": "ordinary.safetensors", "strength": 0.3}
    manifest["nsfw_loras"]["inputs"].append("lora_2")
    (tmp_path / "workflow.json").write_text(json.dumps(graph))
    workflow = Workflow.load(manifest, tmp_path, 100000)
    for enabled in (False, True, False):
        built = workflow.build(workflow.normalize({"prompt": "test", "nsfw": enabled}))
        expected = copy.deepcopy(inputs)
        for name in ("lora_1", "lora_2"):
            expected[name]["on"] = enabled
        assert built["11"]["inputs"] == expected
    assert workflow.build(workflow.normalize({"prompt": "test"}))["11"]["inputs"][
        "lora_1"]["on"] is False
    assert workflow.graph == graph


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false", [], {}])
def test_nsfw_requires_a_boolean(tmp_path, value):
    write_catalog(tmp_path)
    manifest = write_nsfw_catalog(tmp_path)
    workflow = Workflow.load(manifest, tmp_path, 100000)
    with pytest.raises(WorkflowError, match="nsfw"):
        workflow.normalize({"prompt": "test", "nsfw": value})


@pytest.mark.parametrize("targets", [
    None, {}, {"node": "11", "inputs": []}, {"node": "missing", "inputs": ["lora_1"]},
    {"node": "11", "inputs": ["lora_1", "lora_1"]}, {"node": "11", "inputs": [None]},
    {"node": "11", "inputs": ["show_strengths"]}, {"node": "11", "inputs": ["lora_99"]},
    {"node": "11", "inputs": ["lora_1"], "default": True},
])
def test_invalid_nsfw_targets_fail_at_catalog_load(tmp_path, targets):
    write_catalog(tmp_path)
    manifest = write_nsfw_catalog(tmp_path)
    manifest["nsfw_loras"] = targets
    with pytest.raises(WorkflowError):
        Workflow.load(manifest, tmp_path, 100000)


@pytest.mark.parametrize("operation", ["set", "remove"])
def test_modes_cannot_override_nsfw_rows(tmp_path, operation):
    write_catalog(tmp_path)
    manifest = write_nsfw_catalog(tmp_path)
    modes = json.loads((tmp_path / "modes.yaml").read_text())
    mode = modes["modes"]["turbo"]
    mode.setdefault(operation, {})["11"] = (
        {"lora_1": {"on": True}} if operation == "set" else ["lora_1"]
    )
    (tmp_path / "modes.yaml").write_text(json.dumps(modes))
    with pytest.raises(WorkflowError, match="conflicts"):
        Workflow.load(manifest, tmp_path, 100000)


def test_agent_parameters_cannot_replace_nsfw_row_or_default(tmp_path):
    write_catalog(tmp_path)
    manifest = write_nsfw_catalog(tmp_path)
    parameter = {"type": "boolean", "default": True,
                 "target": {"node": "11", "input": "lora_1"}}
    manifest["parameters"]["nsfw"] = parameter
    with pytest.raises(WorkflowError, match="reserved"):
        Workflow.load(manifest, tmp_path, 100000)
    manifest["parameters"]["other"] = manifest["parameters"].pop("nsfw")
    with pytest.raises(WorkflowError, match="conflicts"):
        Workflow.load(manifest, tmp_path, 100000)


def test_reference_mapping_cannot_replace_nsfw_row(tmp_path):
    write_catalog(tmp_path)
    manifest = write_nsfw_catalog(tmp_path)
    manifest["references"] = {
        "adapter": "helto_root_images", "generator": "11", "loader": "9",
        "inputs": ["lora_1"],
    }
    with pytest.raises(WorkflowError, match="reference controls"):
        Workflow.load(manifest, tmp_path, 100000)


def test_unconfigured_workflow_does_not_expose_nsfw(tmp_path):
    _, manifest = write_catalog(tmp_path)
    workflow = Workflow.load(manifest, tmp_path, 100000)
    assert "nsfw" not in workflow.describe()["parameters"]["properties"]
    with pytest.raises(WorkflowError, match="unknown"):
        workflow.normalize({"nsfw": True})


class NSFWTransportTests(unittest.IsolatedAsyncioTestCase):
    url = transport.MCPTransportTests.url
    sdk_client = transport.MCPTransportTests.sdk_client

    async def asyncSetUp(self):
        await fixtures.MediaJobsTests.asyncSetUp(self)
        write_nsfw_catalog(self.catalog_dir)
        self.jobs.catalog = WorkflowCatalog(str(self.catalog_dir), 100000)

    def submitted_nsfw(self):
        return [body["prompt"]["11"]["inputs"]["lora_1"]["on"]
                for body in self.comfy.submissions]

    async def test_single_nsfw_discovery_activation_default_and_retry_identity(self):
        async with self.sdk_client() as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            for name in ("generate", "generate_batch"):
                self.assertIn("explicitly requests", tools[name].description)
            description = await client.call_tool("describe_workflow", {"workflow_id": "example"})
            schema = description.structured_content["parameters"]["properties"]
            self.assertEqual(schema["nsfw"]["type"], "boolean")
            self.assertIs(schema["nsfw"]["default"], False)
            for index, flag in enumerate(({}, {"nsfw": True}, {})):
                arguments = {"job_id": f"single-{index}", "workflow_id": "example",
                             "parameters": {"prompt": "test", **flag}}
                result = await client.call_tool("generate", arguments)
                self.assertFalse(result.is_error, result)
                self.assertEqual(result.structured_content["parameters"]["nsfw"], bool(flag))
                retry = await client.call_tool("generate", {
                    **arguments, "parameters": {**arguments["parameters"], "nsfw": bool(flag)},
                })
                self.assertEqual(retry.structured_content, result.structured_content)
                recovered = await client.call_tool("get_job", {"job_id": f"single-{index}"})
                self.assertEqual(recovered.structured_content, result.structured_content)
            conflict = await client.call_tool("generate", {
                "job_id": "single-1", "workflow_id": "example", "parameters": {"prompt": "test"},
            })
            self.assertTrue(conflict.is_error)
            self.assertEqual(conflict.structured_content["error"]["category"], "conflict")
        self.assertEqual(self.submitted_nsfw(), [False, True, False])

    async def test_mixed_batch_nsfw_envelopes_metadata_and_retry_identity(self):
        items = [
            {"prompt": "one", "nsfw": True, "mode": "quality"},
            {"parameters": {"prompt": "two", "mode": "turbo"}, "references": []},
            {"parameters": {"prompt": "three", "nsfw": True, "mode": "turbo"}},
            {"prompt": "four", "nsfw": False, "mode": "quality"},
        ]
        arguments = {"job_id": "mixed", "workflow_id": "example", "items": items}
        async with self.sdk_client() as client:
            result = await client.call_tool("generate_batch", arguments)
            self.assertFalse(result.is_error, result)
            self.assertEqual(result.structured_content["status"], "succeeded")
            self.assertEqual([item["parameters"]["nsfw"]
                              for item in result.structured_content["items"]],
                             [True, False, True, False])
            retry_items = copy.deepcopy(items)
            retry_items[1]["parameters"]["nsfw"] = False
            retry = await client.call_tool("generate_batch", {**arguments, "items": retry_items})
            self.assertEqual(retry.structured_content, result.structured_content)
            retry_items[1]["parameters"]["nsfw"] = True
            conflict = await client.call_tool("generate_batch", {
                **arguments, "items": retry_items,
            })
            self.assertTrue(conflict.is_error)
            self.assertEqual(conflict.structured_content["error"]["category"], "conflict")
        self.assertEqual(self.submitted_nsfw(), [True, False, True, False])
        self.assertEqual(self.llm.admin_calls, ["unload_model"])

    async def test_invalid_single_and_late_batch_flags_never_queue(self):
        async with self.sdk_client() as client:
            for tool, arguments in (
                ("generate", {"parameters": {"prompt": "one", "nsfw": 1}}),
                ("generate_batch", {"items": [
                    {"prompt": "one", "nsfw": True},
                    {"parameters": {"prompt": "two", "nsfw": "true"}},
                ]}),
            ):
                result = await client.call_tool(tool, {
                    "job_id": tool, "workflow_id": "example", **arguments,
                })
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["error"]["category"], "validation")
        self.assertFalse(self.comfy.submissions)
        self.assertFalse(self.llm.admin_calls)
        self.assertFalse(self.jobs.jobs)
