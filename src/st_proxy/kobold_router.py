"""Small wire-format helpers for KoboldCpp's native router (no model routing)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import ConfigurationError

# These are the native router's text wake endpoints. Normalize legacy aliases
# before forwarding so token counting and generation share its model selection.
TEXT_PATHS = {
    "/v1/chat/completions": "/v1/chat/completions",
    "/chat/completions": "/chat/completions",
    "/v1/completions": "/v1/completions",
    "/v1/completion": "/v1/completion",
    "/completions": "/completions",
    "/api/v1/generate": "/api/v1/generate",
    "/api/latest/generate": "/api/v1/generate",
    "/api/extra/generate/stream": "/api/extra/generate/stream",
    "/api/extra/tokencount": "/api/extra/tokencount",
    "/api/extra/tokenize": "/api/extra/tokencount",
}
ABORT_PATH = "/api/extra/abort"


def model_list(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("invalid KoboldCpp model list")
    ids = {
        item["id"]
        for item in payload["data"]
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and (item["id"] == "initial_model" or item["id"].endswith(".kcpps"))
    }
    if "initial_model" not in ids:
        raise ValueError("KoboldCpp Router mode model list is unavailable")
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "owned_by": "koboldcpp"}
            for name in sorted(ids, key=lambda name: (name != "initial_model", name))
        ],
    }


def read_model_cache(filename: str | None) -> dict[str, Any]:
    if filename is None:
        return model_list({"data": [{"id": "initial_model"}]})
    try:
        return model_list(json.loads(Path(filename).read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise ConfigurationError("cannot read KoboldCpp model cache") from exc


def default_model_body(body: bytes) -> bytes:
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    if payload.get("model") in (None, ""):
        payload["model"] = "initial_model"
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return body
