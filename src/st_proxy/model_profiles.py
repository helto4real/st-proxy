"""Offline, exact-model defaults for Chat Completions. Caller values always win."""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

from .errors import ConfigurationError

LOG = logging.getLogger(__name__)
STRATA = frozenset({"sc117", "orca", "sc117_128k", "orca_128k"})
BACKENDS = STRATA | {"prism", "tabbyapi"}
PARAMETERS = frozenset({
    "temperature", "top_p", "top_k", "min_p", "repetition_penalty",
    "presence_penalty", "frequency_penalty", "reasoning_budget_tokens",
})
REPETITION_ALIASES = ("repetition_penalty", "repeat_penalty", "rep_pen")
BUDGET_ALIASES = (
    "reasoning_budget_tokens", "reasoning_budget", "thinking_budget",
    "thinking_token_budget", "thinking_budget_tokens",
)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key}")
        result[key] = value
    return result


def _validate_parameters(values: Any, engines: set[str], where: str) -> None:
    if not isinstance(values, dict) or values.keys() - PARAMETERS:
        raise ValueError(f"{where}: expected supported parameter names")
    for key, value in values.items():
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"{where}.{key}: expected a finite number")
        if key == "temperature":
            valid = 0 <= value <= 2
        elif key == "top_p":
            valid = 0 < value <= 1
        elif key == "min_p":
            valid = 0 <= value <= 1
        elif key == "repetition_penalty":
            valid = value > 0
        elif key in {"top_k", "reasoning_budget_tokens"}:
            valid = type(value) is int and value >= 0
            if key == "top_k" and engines & STRATA:
                valid = valid and 1 <= value <= 64
        else:
            valid = (-2 if not engines & STRATA else 0) <= value <= 2
        if not valid:
            raise ValueError(f"{where}.{key}: value outside supported backend range")


def _mode(payload: dict[str, Any], engine: str) -> str | None:
    """Conservative recognition of explicit backend controls, never a thinking override.

    Absent, null, conflicting, or unrecognised controls cannot select a mode.
    Budget size alone does not imply thinking on/off.
    """
    modes: set[str] = set()
    controls: list[tuple[str, Any]] = []
    if "reasoning_effort" in payload:
        controls.append(("effort", payload["reasoning_effort"]))
    if "enable_thinking" in payload:
        # Not an OpenAI Chat Completions control on the installed Strata/Prism builds.
        if engine != "tabbyapi":
            return None
        controls.append(("enabled", payload["enable_thinking"]))
    for key in ("reasoning", "chat_template_kwargs", "template_vars", "thinking"):
        if key not in payload:
            continue
        if engine == "tabbyapi" and key == "chat_template_kwargs" and "template_vars" in payload:
            continue  # Tabby's native template_vars takes precedence as a whole.
        obj = payload[key]
        if not isinstance(obj, dict):
            return None
        if key == "thinking" or (key == "template_vars" and engine != "tabbyapi"):
            return None
        effort_key = "effort" if key == "reasoning" else "reasoning_effort"
        enabled_key = "enabled" if key == "reasoning" else "enable_thinking"
        for name, value in obj.items():
            if name == effort_key:
                if key == "reasoning" and engine == "prism":
                    return None
                if engine == "prism" and value == "none":
                    return None  # Only the top-level field disables Prism thinking.
                controls.append(("effort", value))
            elif name == enabled_key:
                if key == "reasoning" and engine != "tabbyapi":
                    return None
                if engine in STRATA and value is True:
                    continue  # Strata only consumes an explicit false toggle here.
                controls.append(("enabled", value))
    for kind, value in controls:
        if kind == "enabled":
            if type(value) is not bool:
                return None
            modes.add("thinking" if value else "non_thinking")
        elif isinstance(value, str):
            effort = value if engine == "prism" else value.strip().lower()
            if effort in ({"none"} if engine == "prism" else {"none", "off"}):
                modes.add("non_thinking")
            elif effort in ({"low", "medium", "xhigh"} if engine == "prism"
                            else {"low", "medium", "high", "xhigh"}):
                modes.add("thinking")
            else:
                return None
        else:
            return None
    return next(iter(modes)) if len(modes) == 1 else None


def _has_thinking_control(payload: dict[str, Any]) -> bool:
    return any(key in payload for key in (
        *BUDGET_ALIASES, "reasoning_effort", "enable_thinking", "reasoning", "thinking",
    )) or any(
        key in payload and (
            not isinstance(payload[key], dict)
            or any(field in payload[key] for field in (
                "enable_thinking", "reasoning_effort", *BUDGET_ALIASES,
            ))
        ) for key in ("chat_template_kwargs", "template_vars")
    )


class ModelProfiles:
    def __init__(self, path: str | None = None) -> None:
        self._models: dict[str, dict[str, Any]] = {}
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_object)
            if (not isinstance(data, dict) or set(data) != {"version", "profiles"}
                    or type(data["version"]) is not int or data["version"] != 1
                    or not isinstance(data["profiles"], list)):
                raise ValueError("expected version 1 and a profiles list")
            names = set()
            for profile in data["profiles"]:
                self._add(profile, names)
        except (OSError, ValueError, TypeError, RecursionError, OverflowError) as exc:
            raise ConfigurationError(f"model profiles {path}: {exc}") from exc

    def _add(self, profile: Any, names: set[str]) -> None:
        fields = {"id", "models", "defaults", "thinking", "non_thinking", "sources",
                  "checked_at", "notes", "installed_revision"}
        if not isinstance(profile, dict) or profile.keys() - fields:
            raise ValueError("unknown profile fields")
        name, models = profile.get("id"), profile.get("models")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("profile IDs must be nonempty and unique")
        names.add(name)
        if not isinstance(models, list) or not models:
            raise ValueError(f"{name}: models must be a nonempty list")
        engines = set()
        for model in models:
            if (not isinstance(model, str) or "/" not in model
                    or model.split("/", 1)[0] not in BACKENDS
                    or not model.split("/", 1)[1].strip()
                    or any(c in model for c in "*?[]")):
                raise ValueError(f"{name}: expected an exact supported backend/model ID")
            if model in self._models:
                raise ValueError(f"duplicate model match: {model}")
            self._models[model] = profile
            engines.add(model.split("/", 1)[0])
        for key in ("defaults", "thinking", "non_thinking"):
            _validate_parameters(profile.get(key, {}), engines, f"{name}.{key}")
        for key in ("checked_at", "notes", "installed_revision"):
            if key in profile and not isinstance(profile[key], str):
                raise ValueError(f"{name}.{key}: expected text")
        sources = profile.get("sources", [])
        if not isinstance(sources, list) or any(not isinstance(s, str) for s in sources):
            raise ValueError(f"{name}.sources: expected a list of source URLs")

    def apply(self, payload: dict[str, Any], engine: str, model: Any) -> dict[str, Any]:
        if not isinstance(model, str):
            return payload
        profile = self._models.get(f"{engine}/{model}")
        if profile is None:
            return payload
        mode = _mode(payload, engine)
        defaults = {**profile.get("defaults", {}), **profile.get(mode, {})}
        result = payload.copy()
        added = []
        for key, value in defaults.items():
            if key == "reasoning_budget_tokens" and _has_thinking_control(payload):
                continue
            wire = "repeat_penalty" if key == "repetition_penalty" and engine == "prism" else key
            aliases = REPETITION_ALIASES if key == "repetition_penalty" else (wire,)
            if any(alias in payload for alias in aliases):
                continue
            result[wire] = value
            added.append(wire)
        # Translate a caller's repetition alias without changing/removing its value.
        # Existing native fields (including null/invalid values) always have precedence.
        wire = "repeat_penalty" if engine == "prism" else "repetition_penalty"
        if wire not in payload:
            aliases = ("rep_pen", "repeat_penalty") if engine == "tabbyapi" else REPETITION_ALIASES
            alias = next((key for key in aliases if key in payload), None)
            if alias is not None:
                result[wire] = payload[alias]
        if added:
            LOG.debug("model profile=%s mode=%s default_fields=%s",
                      profile["id"], mode or "unknown", ",".join(added))
        return result if result != payload else payload

    def apply_body(self, body: bytes, engine: str, model: Any = None) -> bytes:
        if not self._models:
            return body
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            return body
        if not isinstance(payload, dict):
            return body
        result = self.apply(payload, engine, payload.get("model") if model is None else model)
        return body if result is payload else json.dumps(result, ensure_ascii=False).encode()
