from __future__ import annotations

from enum import StrEnum


class ComfyRouteKind(StrEnum):
    PASSTHROUGH = "passthrough"
    WORKFLOW = "workflow"
    CONTROL = "control"
    LIFECYCLE = "lifecycle"
    UNKNOWN_MUTATION = "unknown_mutation"


WORKFLOW_PATHS = frozenset({"/prompt", "/api/prompt"})
CONTROL_PATHS = frozenset({"/interrupt", "/queue"})
LIFECYCLE_PATHS = frozenset({"/free"})

# ComfyUI's frontend uses these /api aliases while older backend versions only
# expose the corresponding unprefixed routes. Keep this list narrow: some
# routes, such as /api/jobs and /api/assets, are genuine API-only endpoints.
EXACT_UPSTREAM_ROUTE_ALIASES = {
    "/api/prompt": "/prompt",
    "/api/users": "/users",
}
PREFIX_UPSTREAM_ROUTE_ALIASES = {
    "/api/settings": "/settings",
    "/api/userdata": "/userdata",
}

# These routes mutate files or user-interface state but do not execute a workflow.
# Prefixes cover the corresponding per-user and per-resource forms.
SAFE_MUTATION_PATHS = frozenset(
    {
        "/api/assets",
        "/api/helto_director/h3_preview/decrypt",
        "/api/settings",
        "/api/userdata",
        "/api/users",
        "/helto_director/privacy/decrypt",
        "/helto_director/privacy/encrypt",
        "/helto_director/privacy/keystore/change_password",
        "/helto_director/privacy/keystore/init",
        "/helto_director/privacy/lock",
        "/helto_director/privacy/unlock",
        "/helto_director/h3_preview/decrypt",
        "/helto_privacy/keystore/change_password",
        "/helto_privacy/keystore/init",
        "/helto_privacy/lock",
        "/helto_privacy/unlock",
        "/history",
        "/settings",
        "/upload/image",
        "/upload/mask",
        "/userdata",
        "/users",
    }
)
SAFE_MUTATION_PREFIXES = (
    "/api/assets/",
    "/api/settings/",
    "/api/userdata/",
    "/settings/",
    "/userdata/",
    "/users/",
)


def comfy_upstream_path(path: str) -> str:
    if alias := EXACT_UPSTREAM_ROUTE_ALIASES.get(path):
        return alias
    for public_prefix, upstream_prefix in PREFIX_UPSTREAM_ROUTE_ALIASES.items():
        if path == public_prefix:
            return upstream_prefix
        if path.startswith(public_prefix + "/"):
            return upstream_prefix + path[len(public_prefix) :]
    return path


def _is_job_cancel(path: str) -> bool:
    if path == "/api/jobs/cancel":
        return True
    parts = path.strip("/").split("/")
    return len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cancel"


def classify_comfy_route(method: str, path: str) -> ComfyRouteKind:
    normalized_method = method.upper()
    if normalized_method in {"GET", "HEAD", "OPTIONS"}:
        return ComfyRouteKind.PASSTHROUGH
    if normalized_method == "POST" and path in WORKFLOW_PATHS:
        return ComfyRouteKind.WORKFLOW
    if normalized_method == "POST" and (path in CONTROL_PATHS or _is_job_cancel(path)):
        return ComfyRouteKind.CONTROL
    if normalized_method == "POST" and path in LIFECYCLE_PATHS:
        return ComfyRouteKind.LIFECYCLE
    if path in SAFE_MUTATION_PATHS or path.startswith(SAFE_MUTATION_PREFIXES):
        return ComfyRouteKind.PASSTHROUGH
    return ComfyRouteKind.UNKNOWN_MUTATION
