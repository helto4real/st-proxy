from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from st_proxy.workflows import Workflow, WorkflowCatalog, WorkflowError

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_workflow.json"


def write_catalog(directory: Path, *, helto=True):
    directory.mkdir(parents=True, exist_ok=True)
    graph = json.loads(FIXTURE.read_text())
    graph["3"]["inputs"].update(folder=str(directory / "source"),
                               alternative_folder=str(directory / "source"))
    manifest = {
        "id": "example", "name": "Example", "description": "Synthetic image workflow",
        "workflow": "workflow.json", "parameters": {
            "prompt": {"type": "string", "required": True, "minLength": 1,
                       "maxLength": 4000, "private": True,
                       "target": {"node": "1", "input": "positive_prompt"}},
            "seed": {"type": "integer", "default": 42, "minimum": 0,
                     "maximum": 9007199254740991, "target": {"node": "1", "input": "seed"}},
        },
        "outputs": [{"node": "3", "adapter": "helto_saved_images" if helto else "comfy_images"}],
    }
    (directory / "workflow.json").write_text(json.dumps(graph))
    (directory / "example.manifest.json").write_text(json.dumps(manifest))
    return graph, manifest


def test_only_mapped_inputs_and_required_outputs_are_validated(tmp_path):
    graph, manifest = write_catalog(tmp_path)
    graph["future-node"] = {"class_type": "UninstalledFutureNode", "inputs": {
        "arbitrary": {"deeply": ["nested", {"anything": True}]},
        "future-link": ["not-currently-present", 17],
    }}
    graph["9"] = {"future_data": [1, 2, 3]}
    graph["1"]["inputs"]["new input"] = {"future": "value"}
    graph["1"]["class_type"] = "ACompatibleFutureGenerator"
    graph["export-extension"] = ["opaque", "data"]
    (tmp_path / "workflow.json").write_text(json.dumps(graph))
    workflow = WorkflowCatalog(str(tmp_path), 100000).get("example")
    values = workflow.normalize({"prompt": "new prompt"})
    actual = workflow.build(values)
    expected = copy.deepcopy(graph)
    expected["1"]["inputs"].update(positive_prompt="new prompt", seed=42)
    expected["3"]["inputs"]["reference_only"] = True
    assert actual == expected
    assert workflow.graph == graph
    assert json.loads((tmp_path / "workflow.json").read_text()) == graph
    assert "target" not in json.dumps(workflow.describe())


@pytest.mark.parametrize("parameters", [
    {}, {"prompt": ""}, {"prompt": "x" * 4001}, {"prompt": "x", "seed": True},
    {"prompt": "x", "seed": -1}, {"prompt": "x", "arbitrary_node": 2},
    {"prompt": 123}, {"prompt": "x", "seed": float("nan")},
])
def test_bad_agent_parameters_are_rejected(tmp_path, parameters):
    write_catalog(tmp_path)
    workflow = WorkflowCatalog(str(tmp_path), 100000).get("example")
    with pytest.raises(WorkflowError):
        workflow.normalize(parameters)


@pytest.mark.parametrize("break_contract", ["input", "output", "save", "duplicate", "path"])
def test_missing_configured_contracts_are_unavailable(tmp_path, break_contract):
    graph, manifest = write_catalog(tmp_path)
    if break_contract == "input":
        del graph["1"]["inputs"]["seed"]
    elif break_contract == "output":
        del graph["3"]
    elif break_contract == "save":
        graph["3"]["inputs"]["save_image"] = False
    elif break_contract == "duplicate":
        (tmp_path / "duplicate.manifest.json").write_text(json.dumps(manifest))
    else:
        manifest["workflow"] = "../outside.json"
    (tmp_path / "workflow.json").write_text(json.dumps(graph))
    (tmp_path / "example.manifest.json").write_text(json.dumps(manifest))
    assert not WorkflowCatalog(str(tmp_path), 100000).workflows


def test_bad_manifest_does_not_hide_other_workflows_or_log_prompt(tmp_path, caplog):
    write_catalog(tmp_path)
    (tmp_path / "bad.manifest.json").write_text('{"secret": "SYNTHETIC_SECRET"')
    assert WorkflowCatalog(str(tmp_path), 100000).get("example")
    assert "SYNTHETIC_SECRET" not in caplog.text


def test_plain_output_adapter_does_not_require_a_specific_node_class(tmp_path):
    graph, _ = write_catalog(tmp_path, helto=False)
    graph["3"] = {"class_type": "FutureSaveImages", "inputs": {"new": "anything"}}
    (tmp_path / "workflow.json").write_text(json.dumps(graph))
    workflow = WorkflowCatalog(str(tmp_path), 100000).get("example")
    assert workflow.build(workflow.normalize({"prompt": "x"}))["3"] == graph["3"]


def test_image_manifest_maps_sizes_and_preserves_other_inputs(tmp_path):
    graph, _ = write_catalog(tmp_path)
    manifest_path = Path(__file__).resolve().parents[1] / "workflows/image_generation.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["workflow"] = "workflow.json"
    shipped = json.loads((manifest_path.parent / "image_generation.json").read_text())
    graph["11"]["inputs"]["lora_2"] = shipped["11"]["inputs"]["lora_2"]
    (tmp_path / "workflow.json").write_text(json.dumps(graph))
    guidance = tmp_path / manifest["prompt_guidance"]
    guidance.parent.mkdir(parents=True)
    guidance.write_bytes((manifest_path.parent / manifest["prompt_guidance"]).read_bytes())
    modes = tmp_path / manifest["modes"]
    modes.parent.mkdir(parents=True)
    modes.write_bytes((manifest_path.parent / manifest["modes"]).read_bytes())
    workflow = Workflow.load(manifest, tmp_path, 100000)
    assert workflow.normalize({"prompt": "test"}) == {
        "prompt": "test", "aspect_ratio": "16:9", "max_side": 1600, "mode": "standard",
        "nsfw": False,
    }
    values = workflow.normalize({"prompt": "test", "aspect_ratio": "9:16", "max_side": 1536,
                                 "mode": "quick draft", "seed": graph["1"]["inputs"]["seed"]})
    expected = copy.deepcopy(graph)
    expected["1"]["inputs"].update({
        "positive_prompt": "test", "aspect ratio": "9:16", "max side": 1536,
    })
    expected["3"]["inputs"]["reference_only"] = True
    expected["1"]["inputs"]["diffusion_model"] = shipped["1"]["inputs"]["diffusion_model"]
    assert workflow.build(values) == expected
    schema = workflow.describe()["parameters"]["properties"]
    assert schema["max_side"]["multipleOf"] == 32
    assert schema["aspect_ratio"]["enum"] == [
        "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9",
    ]
    with pytest.raises(WorkflowError):
        workflow.normalize({"prompt": "test", "max_side": 1000})
    with pytest.raises(WorkflowError):
        workflow.normalize({"prompt": "test", "aspect_ratio": "21:9"})
    manifest["parameters"]["max_side"]["multipleOf"] = 0
    with pytest.raises(WorkflowError):
        Workflow.load(manifest, tmp_path, 100000)
