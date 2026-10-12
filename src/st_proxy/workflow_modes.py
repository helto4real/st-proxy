"""Startup-only, operator-owned input patches for API workflows."""
from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ModeError(ValueError):
    """Safe to log without revealing YAML contents or local paths."""


def _parse(raw: bytes, max_bytes: int) -> dict[str, Any]:
    # YAML is optional when MCP is not installed/enabled.
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    Loader.yaml_implicit_resolvers = {
        key: [(tag, pattern) for tag, pattern in entries
              if tag not in {"tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp"}]
        for key, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    Loader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(true|false)$"),
                                 list("tf"))

    def mapping(loader, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or not key or key in result:
                raise ModeError("mode YAML needs unique, nonempty string keys")
            result[key] = loader.construct_object(value_node, deep=deep)
        return result

    Loader.add_constructor("tag:yaml.org,2002:map", mapping)
    try:
        data = yaml.load(raw, Loader=Loader)
    except (yaml.YAMLError, UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, ModeError):
            raise
        raise ModeError("invalid mode YAML") from None

    # Bound expanded aliases and reject cycles/non-JSON values before copying patches.
    remaining = max_bytes

    def check(value, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 64:
            raise ModeError("mode YAML is too large or deeply nested")
        if type(value) is dict:
            for child in value.values():
                check(child, depth + 1)
        elif type(value) is list:
            for child in value:
                check(child, depth + 1)
        elif type(value) not in (str, int, float, bool, type(None)):
            raise ModeError("mode values must be JSON-compatible")
        elif type(value) is float and not math.isfinite(value):
            raise ModeError("mode values must be finite")

    check(data)
    size = 0
    for chunk in json.JSONEncoder(ensure_ascii=True, allow_nan=False).iterencode(data):
        size += len(chunk)  # ASCII encoding: characters and bytes have identical size.
        if size > max_bytes:
            raise ModeError("expanded mode YAML is too large")
    if not isinstance(data, dict):
        raise ModeError("mode YAML must be an object")
    return data


@dataclass(frozen=True)
class WorkflowModes:
    default: str
    definitions: dict[str, dict[str, Any]]

    @classmethod
    def read(cls, path: Path, max_bytes: int) -> WorkflowModes:
        try:
            with path.open("rb") as stream:
                raw = stream.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise ModeError("mode YAML is too large")
            data = _parse(raw, max_bytes)
        except (OSError, UnicodeError, OverflowError, RecursionError):
            raise ModeError("unreadable mode definition") from None
        if (data.keys() != {"version", "default", "modes"}
                or type(data["version"]) is not int or data["version"] != 1
                or not isinstance(data["default"], str)
                or not isinstance(data["modes"], dict) or not data["modes"]
                or data["default"] not in data["modes"]):
            raise ModeError("invalid mode version, default or modes")
        for mode in data["modes"].values():
            if (not isinstance(mode, dict) or mode.keys() - {"description", "set", "remove"}
                    or not isinstance(mode.get("description"), str)
                    or not mode["description"].strip()):
                raise ModeError("mode needs a description and optional set/remove patches")
            for operation in ("set", "remove"):
                patches = mode.get(operation, {})
                if not isinstance(patches, dict):
                    raise ModeError("mode patches must map node IDs to inputs")
                for inputs in patches.values():
                    if operation == "set":
                        if not isinstance(inputs, dict):
                            raise ModeError("mode set must map input names to values")
                    elif (not isinstance(inputs, list)
                          or any(not isinstance(name, str) or not name for name in inputs)
                          or len(set(inputs)) != len(inputs)):
                        raise ModeError("mode remove must list unique input names")
            for node, names in mode.get("remove", {}).items():
                if set(names) & mode.get("set", {}).get(node, {}).keys():
                    raise ModeError("mode cannot set and remove the same input")
        return cls(data["default"], data["modes"])

    def validate_targets(self, graph: dict, parameters: dict, outputs: list,
                         references: dict | None,
                         controlled_inputs: set[tuple[str, str]] | None = None) -> None:
        mapped = {(spec["target"]["node"], spec["target"]["input"])
                  for spec in parameters.values()}
        mapped.update(controlled_inputs or ())
        protected_nodes = {output["node"] for output in outputs}
        if references:
            protected_nodes.add(references["loader"])
            mapped.update((references["generator"], name) for name in references["inputs"])
        for mode in self.definitions.values():
            for operation in ("set", "remove"):
                for node, inputs in mode.get(operation, {}).items():
                    target = graph.get(node)
                    if not isinstance(target, dict) or not isinstance(target.get("inputs"), dict):
                        raise ModeError("configured mode node or its inputs are missing")
                    if node in protected_nodes or any((node, name) in mapped for name in inputs):
                        raise ModeError("mode conflicts with parameter or adapter controls")

    def parameter(self) -> dict[str, Any]:
        return {"type": "string", "default": self.default, "enum": list(self.definitions),
                "description": "Generation mode. " + " ".join(
                    f"{name}: {mode['description']}" for name, mode in self.definitions.items())}

    def apply(self, graph: dict, name: str) -> None:
        if not isinstance(name, str) or name not in self.definitions:
            raise ModeError("unknown generation mode")
        mode = self.definitions[name]
        for node, inputs in mode.get("set", {}).items():
            graph[node]["inputs"].update(copy.deepcopy(inputs))
        for node, names in mode.get("remove", {}).items():
            for field in names:
                graph[node]["inputs"].pop(field, None)


class ModeFiles:
    """One file snapshot per catalog, including failures; validation remains per workflow."""

    def __init__(self) -> None:
        self.files: dict[Path, WorkflowModes | ModeError] = {}

    def load(self, filename: str, directory: Path, max_bytes: int) -> WorkflowModes:
        if not isinstance(filename, str) or not filename.strip():
            raise ModeError("modes must name a local YAML file")
        path = (directory / filename).resolve()
        if not path.is_relative_to(directory.resolve()):
            raise ModeError("mode YAML must remain inside the workflow directory")
        if path not in self.files:
            try:
                self.files[path] = WorkflowModes.read(path, max_bytes)
            except ModeError as exc:
                self.files[path] = exc
        result = self.files[path]
        if isinstance(result, ModeError):
            raise result
        return result
