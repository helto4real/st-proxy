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

# These routes mutate files or user-interface state but do not execute a workflow.
# Prefixes cover the corresponding per-user and per-resource forms.
SAFE_MUTATION_PATHS = frozenset(
    {
        "/api/assets",
        "/api/settings",
        "/api/userdata",
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
