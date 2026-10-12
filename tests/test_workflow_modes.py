from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from st_proxy.workflow_modes import WorkflowModes
from st_proxy.workflows import Workflow, WorkflowCatalog, WorkflowError

from .test_reference_images import write_references
from .test_workflows import write_catalog

ROOT = Path(__file__).resolve().parents[1] / "workflows"


def write_modes(directory):
    """Add synthetic modes to an existing test manifest."""
    path = directory / "example.manifest.json"
    manifest = json.loads(path.read_text())
    manifest["modes"] = "modes.yaml"
    path.write_text(json.dumps(manifest))
    data = {"version": 1, "default": "turbo", "modes": {
        "turbo": {"description": "Synthetic turbo", "set": {
            "1": {"steps": 8, "cfg": 1, "sigmas": ["13", 0]},
        }},
        "quality": {"description": "Synthetic quality", "set": {
            "1": {"steps": 20, "cfg": 3.5, "diffusion_model": "synthetic-quality"},
            "11": {"lora_1": {"on": True, "lora": "quality-lora", "strength": 0.7},
                   "lora_2": {"on": False, "lora": "second-lora", "strength": 1}},
        }, "remove": {"1": ["sigmas"]}},
    }}
    (directory / "modes.yaml").write_text(json.dumps(data))
    return manifest, data


@pytest.mark.parametrize("workflow_id", ["image_generation", "qwen_scene"])
def test_shipped_modes_apply_current_profiles_without_changing_exports(workflow_id):
    workflow = WorkflowCatalog(str(ROOT), 100000).get(workflow_id)
    original = copy.deepcopy(workflow.graph)
    parameters = {"prompt": "test", "seed": original["1"]["inputs"]["seed"]}
    assert workflow.normalize({"prompt": "test"}) == workflow.normalize({
        "prompt": "test", "mode": "standard",
    })
    expected = copy.deepcopy(original)
    expected["1"]["inputs"]["positive_prompt"] = "test"
    expected["3"]["inputs"]["reference_only"] = True
    assert workflow.build(workflow.normalize({**parameters, "mode": "quick draft"})) == expected
    del expected["1"]["inputs"]["sigmas"]
    expected["1"]["inputs"].update({
        "steps": 40, "cfg": 2.5, "sampler": "er_sde", "scheduler": "beta",
        "use_zero_negative_conditioning": False,
        "diffusion_model": "diffusion_models/qwen-image2.1/qwen_image_2.1_int8_convrot.safetensors",
    })
    assert workflow.build(workflow.normalize(parameters)) == expected
    assert workflow.build(workflow.normalize({**parameters, "mode": "standard"})) == expected
    assert workflow.graph == original
    assert original == json.loads((ROOT / workflow.manifest["workflow"]).read_text())
    spec = workflow.describe()["parameters"]["properties"]["mode"]
    assert spec["default"] == "standard"
    assert spec["enum"] == ["quick draft", "standard"]
    assert "Use for quick previews, composition tests and exploring ideas." in spec["description"]
    assert "Default for normal image generation and finished images." in spec["description"]
    assert "diffusion_model" not in json.dumps(workflow.describe())


def test_patches_are_isolated_dynamic_and_compose_with_references(tmp_path):
    graph, _ = write_references(tmp_path)
    manifest, data = write_modes(tmp_path)
    data["modes"]["quality"]["set"]["1"]["future_option"] = {"nested": [1, False, None]}
    data["modes"]["high quality"] = {"description": "Another operator mode"}
    (tmp_path / "modes.yaml").write_text(json.dumps(data))
    workflow = Workflow.load(manifest, tmp_path, 100000)
    references = [{"root": "/synthetic", "subfolder": "references", "filename": "x.png"}]
    quality = workflow.build(workflow.normalize({"prompt": "ref", "mode": "quality"}), references)
    turbo = workflow.build(workflow.normalize({"prompt": "text"}))
    assert "sigmas" not in quality["1"]["inputs"]
    assert quality["1"]["inputs"]["steps"] == 20
    assert quality["11"]["inputs"]["lora_1"] == {
        "on": True, "lora": "quality-lora", "strength": 0.7,
    }
    assert quality["1"]["inputs"]["image 1"] == ["mcp_reference_1_", 0]
    assert quality["mcp_reference_1_"]["inputs"]["filename"] == "x.png"
    assert quality["unmapped"] == graph["unmapped"]
    assert quality["1"]["inputs"]["unmapped_image"] == ["keep", 7]
    assert turbo["1"]["inputs"]["steps"] == 8
    assert turbo["1"]["inputs"]["sigmas"] == ["13", 0]
    assert "future_option" not in turbo["1"]["inputs"]
    assert "lora_2" not in turbo["11"]["inputs"]
    assert turbo["11"] == graph["11"]
    quality["11"]["inputs"]["lora_1"]["strength"] = 99
    again = workflow.build(workflow.normalize({"prompt": "ref", "mode": "quality"}), references)
    assert again["11"]["inputs"]["lora_1"]["strength"] == 0.7
    assert workflow.graph == graph
    assert workflow.normalize({"prompt": "x", "mode": "high quality"})["mode"] == "high quality"
    for invalid in ("unknown", True, None, 1):
        with pytest.raises(WorkflowError):
            workflow.normalize({"prompt": "x", "mode": invalid})


def test_shared_file_snapshot_is_read_once_until_new_catalog(tmp_path):
    write_catalog(tmp_path)
    manifest, data = write_modes(tmp_path)
    second = {**manifest, "id": "second"}
    (tmp_path / "second.manifest.json").write_text(json.dumps(second))
    with patch.object(WorkflowModes, "read", wraps=WorkflowModes.read) as read:
        catalog = WorkflowCatalog(str(tmp_path), 100000)
        assert read.call_count == 1
        data["default"] = "quality"
        (tmp_path / "modes.yaml").write_text(json.dumps(data))
        for workflow in catalog.workflows.values():
            assert workflow.normalize({"prompt": "x"})["mode"] == "turbo"
            assert workflow.build(workflow.normalize({"prompt": "x"}))["1"]["inputs"]["steps"] == 8
        assert read.call_count == 1
    assert WorkflowCatalog(str(tmp_path), 100000).get("second").normalize({
        "prompt": "x",
    })["mode"] == "quality"


@pytest.mark.parametrize("operation,node,name", [
    (operation, node, name)
    for operation in ("set", "remove")
    for node, name in (("missing", "steps"), ("unmapped", "steps"), ("1", "positive_prompt"),
                       ("1", "seed"), ("3", "save_image"), ("3", "future_output"),
                       ("9", "root"), ("1", "image 10"))
])
def test_only_declared_targets_and_owned_contracts_are_checked(tmp_path, operation, node, name):
    write_references(tmp_path)
    manifest, data = write_modes(tmp_path)
    data["modes"]["quality"] = {"description": "bad", operation: {
        node: {name: 8} if operation == "set" else [name],
    }}
    (tmp_path / "modes.yaml").write_text(json.dumps(data))
    with pytest.raises(WorkflowError):
        Workflow.load(manifest, tmp_path, 100000)


@pytest.mark.parametrize("document", [
    "[]", "version: 2\ndefault: turbo\nmodes: {}", "version: true\ndefault: turbo\nmodes: {}",
    "version: 1\ndefault: missing\nmodes: {turbo: {description: test}}",
    "version: 1\nversion: 1\ndefault: turbo\nmodes: {}",
    'version: 1\ndefault: turbo\nmodes: {turbo: {description: a, description: b}}',
    'version: 1\ndefault: turbo\nmodes: {turbo: {description: x, set: {"1": {cfg: 1, cfg: 2}}}}',
    'version: 1\ndefault: turbo\nmodes: {turbo: {description: x, set: {1: {cfg: 1}}}}',
    'version: 1\ndefault: turbo\nmodes: {turbo: {description: x, set: {"1": {cfg: .nan}}}}',
    'version: 1\ndefault: turbo\nmodes: {turbo: {description: x, set: {"1": {cfg: .inf}}}}',
    'version: 1\ndefault: turbo\nmodes: {turbo: {description: x, set: {"1": {x: !!binary YQ==}}}}',
    'version: 1\ndefault: turbo\nmodes: {turbo: {description: x, remove: {"1": [cfg, cfg]}}}',
    'version: 1\ndefault: turbo\nmodes: {turbo: {description: x, remove: {"1": [null]}}}',
    'version: 1\ndefault: turbo\nmodes: {turbo: {description: x, set: {"1": {cfg: 1}}, '
    'remove: {"1": [cfg]}}}',
    'version: 1\ndefault: turbo\nmodes: {turbo: {description: x, set: {"1": [cfg]}}}',
    'version: 1\ndefault: turbo\nmodes: {turbo: {description: x, sets: {}}}',
    'version: 1\ndefault: turbo\nmodes: {turbo: {set: {}}}',
    '&cycle [*cycle]', '!!python/object/apply:builtins.print [SHOULD_NOT_RUN]',
    'version: [unterminated SECRET_VALUE',
])
def test_bad_yaml_isolated_cached_and_errors_sanitized(tmp_path, caplog, document):
    _, plain = write_catalog(tmp_path)
    manifest, _ = write_modes(tmp_path)
    (tmp_path / "modes.yaml").write_text(document)
    (tmp_path / "second.manifest.json").write_text(json.dumps({**manifest, "id": "second"}))
    (tmp_path / "plain.manifest.json").write_text(json.dumps({**plain, "id": "plain"}))
    with patch.object(WorkflowModes, "read", wraps=WorkflowModes.read) as read:
        catalog = WorkflowCatalog(str(tmp_path), 100000)
        assert read.call_count == 1
    assert list(catalog.workflows) == ["plain"]
    assert "SECRET_VALUE" not in caplog.text


def test_mode_file_path_size_and_reserved_parameter(tmp_path):
    root = tmp_path / "catalog"
    write_catalog(root)
    manifest, _ = write_modes(root)
    outside = tmp_path / "outside.yaml"
    outside.write_bytes((root / "modes.yaml").read_bytes())
    (root / "escape.yaml").symlink_to(outside)
    for filename in ("../outside.yaml", str(outside), "escape.yaml", None, 1, "", "missing.yaml"):
        with pytest.raises(WorkflowError):
            Workflow.load({**manifest, "modes": filename}, root, 100000)
    (root / "modes.yaml").write_text("#" * 100001)
    with pytest.raises(WorkflowError, match="large"):
        Workflow.load(manifest, root, 100000)
    manifest, _ = write_modes(root)
    manifest["parameters"]["mode"] = manifest["parameters"].pop("seed")
    with pytest.raises(WorkflowError, match="reserved"):
        Workflow.load(manifest, root, 100000)


def test_yaml_boolean_keys_are_preserved_and_lora_objects_replace_whole_inputs(tmp_path):
    write_catalog(tmp_path)
    manifest, _ = write_modes(tmp_path)
    (tmp_path / "modes.yaml").write_text('''
version: 1
default: turbo
modes:
  turbo:
    description: true and false are booleans; on and off are strings
    set:
      "11":
        lora_1: {on: true, off: false}
      "1":
        optional: [yes, no, on, off, true, false, 2026-10-11]
''')
    workflow = Workflow.load(manifest, tmp_path, 100000)
    graph = workflow.build(workflow.normalize({"prompt": "x"}))
    assert graph["11"]["inputs"]["lora_1"] == {"on": True, "off": False}
    assert graph["1"]["inputs"]["optional"] == ["yes", "no", "on", "off", True, False, "2026-10-11"]


def test_expanded_aliases_are_bounded_without_rejecting_small_aliases(tmp_path):
    write_catalog(tmp_path)
    manifest, _ = write_modes(tmp_path)
    document = '''
version: 1
default: turbo
modes:
  turbo:
    description: Aliases
    set:
      "1":
        value: &value VALUE
        copies: COPIES
'''
    path = tmp_path / "modes.yaml"
    path.write_text(document.replace("VALUE", "small").replace("COPIES", "[*value, *value]"))
    workflow = Workflow.load(manifest, tmp_path, 100000)
    assert workflow.build(workflow.normalize({"prompt": "x"}))["1"]["inputs"]["copies"] == [
        "small", "small",
    ]
    path.write_text(document.replace("VALUE", "x" * 2000).replace(
        "COPIES", "[" + ", ".join(["*value"] * 1000) + "]",
    ))
    assert path.stat().st_size < 100000
    with pytest.raises(WorkflowError, match="expanded.*large"):
        Workflow.load(manifest, tmp_path, 100000)
