"""Operator-owned workflow contracts; unmapped graph content stays opaque."""
from __future__ import annotations

import copy
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigurationError

LOG = logging.getLogger(__name__)
SCHEMA_KEYS = {"type", "description", "default", "enum", "minimum", "maximum",
               "minLength", "maxLength", "multipleOf"}
PARAMETER_KEYS = SCHEMA_KEYS | {"target", "required", "private"}
TYPES = {"string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,)}


class WorkflowError(ValueError):
    """A sanitized, caller-visible contract error (never include field values)."""


def _check_value(spec: dict[str, Any], value: Any) -> None:
    if type(value) not in TYPES[spec["type"]]:
        raise WorkflowError("parameter has the wrong type")
    if type(value) in (int, float):
        if type(value) is float and not math.isfinite(value):
            raise WorkflowError("parameter must be finite")
        if "minimum" in spec and value < spec["minimum"]:
            raise WorkflowError("parameter is below its minimum")
        if "maximum" in spec and value > spec["maximum"]:
            raise WorkflowError("parameter exceeds its maximum")
        if "multipleOf" in spec and value % spec["multipleOf"] != 0:
            raise WorkflowError("parameter must be a multiple of its configured step")
    if isinstance(value, str):
        if len(value) < spec.get("minLength", 0) or len(value) > spec.get("maxLength", 65536):
            raise WorkflowError("parameter has an invalid length")
    if "enum" in spec and not any(type(value) is type(v) and value == v for v in spec["enum"]):
        raise WorkflowError("parameter is not an allowed choice")


def _inputs(graph: dict[str, Any], node_id: str) -> dict[str, Any]:
    node = graph.get(node_id)
    if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
        raise WorkflowError("configured node or its inputs are missing")
    return node["inputs"]


def _helto_root(inputs: dict[str, Any]) -> Path:
    for field in ("reference_only", "privacy_mode", "save_image", "pause_mode",
                  "use_alternative_folder", "use_date_folder"):
        if type(inputs.get(field)) is not bool:
            raise WorkflowError("Helto output requires boolean save/privacy/reference controls")
    if not inputs["save_image"] or inputs["pause_mode"]:
        raise WorkflowError("Helto output requires save_image=true and pause_mode=false")
    folder = inputs.get("alternative_folder" if inputs["use_alternative_folder"] else "folder")
    if not isinstance(folder, str) or not Path(folder).is_absolute():
        raise WorkflowError("Helto output requires an absolute destination folder")
    subfolder = inputs.get("subfolder", "")
    if (not isinstance(subfolder, str) or Path(subfolder).is_absolute()
            or ".." in Path(subfolder).parts):
        raise WorkflowError("Helto subfolder must remain inside the destination folder")
    return Path(folder).resolve()


@dataclass(frozen=True)
class Workflow:
    manifest: dict[str, Any]
    graph: dict[str, Any]
    source_roots: dict[str, Path]
    prompt_guidance: str | None = None

    @property
    def references(self) -> dict[str, Any] | None:
        return self.manifest.get("references")

    @property
    def id(self) -> str:
        return self.manifest["id"]

    @property
    def parameters(self) -> dict[str, Any]:
        return self.manifest["parameters"]

    @property
    def outputs(self) -> list[dict[str, str]]:
        return self.manifest["outputs"]

    def summary(self) -> dict[str, str]:
        return {k: self.manifest[k] for k in ("id", "name", "description")}

    def describe(self) -> dict[str, Any]:
        properties = {}
        for name, spec in self.parameters.items():
            properties[name] = {k: copy.deepcopy(v) for k, v in spec.items() if k in SCHEMA_KEYS}
        result = {**self.summary(), "parameters": {
            "type": "object", "properties": properties, "additionalProperties": False,
            "required": [k for k, v in self.parameters.items() if v.get("required", False)],
        }, "result_type": "local_image_files"}
        if self.prompt_guidance is not None:
            result["prompt_guidance"] = self.prompt_guidance
        if self.references is not None:
            result["references"] = {
                "minItems": 0, "maxItems": len(self.references["inputs"]),
                "description": "Register images first; ordered image IDs map to <image1>, "
                               "<image2>, etc. An empty list generates without references.",
            }
        return result

    def normalize(self, parameters: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(parameters, dict) or parameters.keys() - self.parameters.keys():
            raise WorkflowError("unknown parameter or invalid parameter object")
        result = {}
        for name, spec in self.parameters.items():
            if name in parameters:
                value = parameters[name]
            elif spec.get("required", False):
                raise WorkflowError(f"missing required parameter: {name}")
            else:
                value = spec["default"]
            try:
                _check_value(spec, value)
            except WorkflowError as exc:
                raise WorkflowError(f"{name}: {exc}") from None
            result[name] = value
        return result

    def build(self, values: dict[str, Any],
              references: list[dict[str, str]] | None = None) -> dict[str, Any]:
        graph = copy.deepcopy(self.graph)
        references = references or []
        spec = self.references
        if references and (spec is None or len(references) > len(spec["inputs"])):
            raise WorkflowError("workflow does not support this number of references")
        if spec is not None:
            inputs = graph[spec["generator"]]["inputs"]
            for name in spec["inputs"]:
                inputs.pop(name, None)
            for index, reference in enumerate(references):
                node_id = f"mcp_reference_{index + 1}"
                while node_id in graph:
                    node_id += "_"
                loader = copy.deepcopy(graph[spec["loader"]])
                loader["inputs"].update(reference)
                graph[node_id] = loader
                inputs[spec["inputs"][index]] = [node_id, 0]
        for name, value in values.items():
            target = self.parameters[name]["target"]
            graph[target["node"]]["inputs"][target["input"]] = value
        for output in self.outputs:
            if output["adapter"] == "helto_saved_images":
                graph[output["node"]]["inputs"]["reference_only"] = True
        return graph

    @classmethod
    def load(cls, manifest: dict[str, Any], directory: Path, max_bytes: int) -> Workflow:
        if not isinstance(manifest, dict):
            raise WorkflowError("manifest must be an object")
        for key in ("id", "name", "description", "workflow"):
            if not isinstance(manifest.get(key), str) or not manifest[key].strip():
                raise WorkflowError("manifest needs id, name, description and workflow")
        path = (directory / manifest["workflow"]).resolve()
        if not path.is_relative_to(directory.resolve()) or path.stat().st_size > max_bytes:
            raise WorkflowError("workflow path or size is invalid")
        graph = json.loads(path.read_text())
        if not isinstance(graph, dict):
            raise WorkflowError("API workflow must be an object")
        parameters, outputs = manifest.get("parameters"), manifest.get("outputs")
        if not isinstance(parameters, dict) or not isinstance(outputs, list) or not outputs:
            raise WorkflowError("manifest needs parameters and outputs")
        mapped = set()
        for name, spec in parameters.items():
            if (not isinstance(name, str) or not name or not isinstance(spec, dict)
                    or spec.keys() - PARAMETER_KEYS or spec.get("type") not in TYPES):
                raise WorkflowError("invalid parameter definition")
            if any(type(spec.get(k, False)) is not bool for k in ("required", "private")):
                raise WorkflowError("required/private must be booleans")
            for key in ("minimum", "maximum", "minLength", "maxLength"):
                if key in spec and (type(spec[key]) not in (int, float)
                                    or not math.isfinite(spec[key])):
                    raise WorkflowError("invalid parameter constraint")
            for key in ("minLength", "maxLength"):
                if key in spec and (type(spec[key]) is not int or spec[key] < 0
                                    or spec["type"] != "string"):
                    raise WorkflowError("invalid string constraint")
            if any(k in spec for k in ("minimum", "maximum")) and spec["type"] not in {
                "number", "integer",
            }:
                raise WorkflowError("numeric constraints require a number")
            if "multipleOf" in spec and (spec["type"] != "integer"
                                         or type(spec["multipleOf"]) is not int
                                         or spec["multipleOf"] <= 0):
                raise WorkflowError("multipleOf requires an integer parameter and positive step")
            for low, high in (("minimum", "maximum"), ("minLength", "maxLength")):
                if low in spec and high in spec and spec[low] > spec[high]:
                    raise WorkflowError("inverted parameter constraints")
            if "enum" in spec:
                if not isinstance(spec["enum"], list) or not spec["enum"]:
                    raise WorkflowError("enum must contain choices")
                for value in spec["enum"]:
                    _check_value(spec, value)
            if "default" in spec:
                _check_value(spec, spec["default"])
                if spec.get("private"):
                    raise WorkflowError("private parameters must be required, without defaults")
            if not spec.get("required", False) and "default" not in spec:
                raise WorkflowError("optional parameters need a default")
            target = spec.get("target")
            if (not isinstance(target, dict) or not isinstance(target.get("node"), str)
                    or not isinstance(target.get("input"), str)):
                raise WorkflowError("invalid parameter target")
            if target["input"] not in _inputs(graph, target["node"]):
                raise WorkflowError("configured input is missing")
            pair = (target["node"], target["input"])
            if pair in mapped:
                raise WorkflowError("parameters cannot target the same input")
            mapped.add(pair)
        roots = {}
        seen = set()
        for output in outputs:
            if (not isinstance(output, dict) or not isinstance(output.get("node"), str)
                    or output.get("adapter") not in {"comfy_images", "helto_saved_images"}):
                raise WorkflowError("invalid output definition")
            node = output["node"]
            if node in seen or not isinstance(graph.get(node), dict):
                raise WorkflowError("output node is missing or duplicated")
            seen.add(node)
            if output["adapter"] == "helto_saved_images":
                if graph[node].get("class_type") != "HeltoSaveImageAdvanced":
                    raise WorkflowError("Helto adapter requires HeltoSaveImageAdvanced")
                if any(target_node == node for target_node, _ in mapped):
                    raise WorkflowError("Helto output controls cannot be agent parameters")
                roots[node] = _helto_root(_inputs(graph, node))
        references = manifest.get("references")
        if references is not None:
            if (not isinstance(references, dict)
                    or references.get("adapter") != "helto_root_images"
                    or not isinstance(references.get("generator"), str)
                    or not isinstance(references.get("loader"), str)
                    or not isinstance(references.get("inputs"), list)):
                raise WorkflowError("invalid reference mapping")
            generator, loader = references["generator"], references["loader"]
            slots = references["inputs"]
            if (not 1 <= len(slots) <= 10
                    or any(not isinstance(s, str) or not s for s in slots)
                    or len(set(slots)) != len(slots) or generator == loader):
                raise WorkflowError("invalid reference inputs")
            _inputs(graph, generator)  # Optional sockets need not appear in an API export.
            loader_inputs = _inputs(graph, loader)
            if (graph[loader].get("class_type") != "HeltoLoadImageFromRoot"
                    or any(not isinstance(loader_inputs.get(k), str)
                           for k in ("root", "subfolder", "filename"))):
                raise WorkflowError("reference adapter requires a HeltoLoadImageFromRoot template")
            if any(node == loader or (node == generator and name in slots)
                   for node, name in mapped):
                raise WorkflowError("reference controls cannot also be parameter targets")
        guidance = None
        if "prompt_guidance" in manifest:
            if not isinstance(manifest["prompt_guidance"], str):
                raise WorkflowError("prompt_guidance must name a local text file")
            path = (directory / manifest["prompt_guidance"]).resolve()
            if not path.is_relative_to(directory.resolve()):
                raise WorkflowError("prompt guidance must remain inside the workflow directory")
            with path.open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise WorkflowError("prompt guidance exceeds 64 KiB")
            guidance = raw.decode("utf-8")
        return cls(copy.deepcopy(manifest), graph, roots, guidance)


class WorkflowCatalog:
    def __init__(self, directory: str, max_bytes: int) -> None:
        root = Path(directory)
        if not root.is_dir():
            raise ConfigurationError("MCP workflow directory does not exist")
        self.workflows: dict[str, Workflow] = {}
        seen = set()
        for path in sorted(root.glob("*.manifest.json")):
            try:
                if path.stat().st_size > max_bytes:
                    raise WorkflowError("manifest is too large")
                manifest = json.loads(path.read_text())
                identifier = manifest.get("id") if isinstance(manifest, dict) else None
                if isinstance(identifier, str):
                    if identifier in seen:
                        self.workflows.pop(identifier, None)
                        raise WorkflowError("duplicate workflow id")
                    seen.add(identifier)
                workflow = Workflow.load(manifest, root, max_bytes)
                self.workflows[workflow.id] = workflow
            except (OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
                reason = str(exc) if isinstance(exc, WorkflowError) else "unreadable definition"
                LOG.error("MCP workflow unavailable: manifest=%s reason=%s", path.name, reason)

    def get(self, workflow_id: str) -> Workflow:
        if workflow_id not in self.workflows:
            raise WorkflowError("unknown or unavailable workflow")
        return self.workflows[workflow_id]
