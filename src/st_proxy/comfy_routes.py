from __future__ import annotations

import re
from enum import StrEnum


class ComfyRouteKind(StrEnum):
    PASSTHROUGH = "passthrough"
    WORKFLOW = "workflow"
    CONTROL = "control"
    LIFECYCLE = "lifecycle"
    BLOCKED_GPU = "blocked_gpu"
    UNKNOWN_MUTATION = "unknown_mutation"


MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

WORKFLOW_ROUTES = frozenset(
    {
        ("POST", "/prompt"),
        ("POST", "/api/prompt"),
    }
)
CONTROL_ROUTES = frozenset(
    {
        ("POST", "/interrupt"),
        ("POST", "/api/interrupt"),
        ("POST", "/queue"),
        ("POST", "/api/queue"),
        ("POST", "/api/helto_save_image_advanced/release"),
        ("POST", "/api/helto_save_video_advanced/release"),
        ("POST", "/helto_prompt_enhancer/providers/unload"),
        ("POST", "/helto_director/prompt_optimizer/models/unload"),
    }
)
LIFECYCLE_ROUTES = frozenset(
    {
        ("POST", "/free"),
        ("POST", "/api/free"),
    }
)
BLOCKED_GPU_ROUTES = frozenset(
    {
        # These Director endpoints may load a model and start work outside
        # ComfyUI's /prompt plus history lifecycle. They need a dedicated
        # coordinator contract before they can safely share the GPU.
        ("POST", "/helto_director/prompt_optimizer/optimize"),
        ("POST", "/helto_director/prompt_optimizer/optimize/start"),
    }
)

# ComfyUI's frontend uses these /api aliases while older backend versions only
# expose the corresponding unprefixed routes. Keep this list narrow: some
# routes, such as /api/jobs and /api/assets, are genuine API-only endpoints.
EXACT_UPSTREAM_ROUTE_ALIASES = {
    "/api/free": "/free",
    "/api/interrupt": "/interrupt",
    "/api/prompt": "/prompt",
    "/api/queue": "/queue",
    "/api/users": "/users",
}
PREFIX_UPSTREAM_ROUTE_ALIASES = {
    "/api/settings": "/settings",
    "/api/userdata": "/userdata",
}

# Core routes mutate files or user-interface state but do not execute a
# workflow. Preserve ComfyUI's supported mutation methods for these stable
# namespaces while keeping reviewed custom-node routes method-specific below.
SAFE_CORE_MUTATION_PATHS = frozenset(
    {
        "/api/assets",
        "/api/settings",
        "/api/userdata",
        "/api/users",
        "/history",
        "/settings",
        "/upload/image",
        "/upload/mask",
        "/userdata",
        "/users",
    }
)
SAFE_CORE_MUTATION_PREFIXES = (
    "/api/assets/",
    "/api/settings/",
    "/api/userdata/",
    "/settings/",
    "/userdata/",
    "/users/",
)

# Reviewed custom routes whose handlers perform only CPU, storage, network, or
# privacy operations. These are public paths as produced by the current ComfyUI
# frontend: apiURL/fetchApi calls include /api, while raw fetch calls do not.
SAFE_CUSTOM_POST_PATHS = frozenset(
    {
        # Shared and pack-specific privacy operations.
        "/api/helto_director/h3_preview/decrypt",
        "/helto_director/h3_preview/decrypt",
        "/helto_director/privacy/decrypt",
        "/helto_director/privacy/encrypt",
        "/helto_director/privacy/keystore/change_password",
        "/helto_director/privacy/keystore/init",
        "/helto_director/privacy/lock",
        "/helto_director/privacy/unlock",
        "/helto_privacy/keystore/change_password",
        "/helto_privacy/keystore/init",
        "/helto_privacy/lock",
        "/helto_privacy/unlock",
        "/helto_spm/privacy/decrypt",
        "/helto_spm/privacy/encrypt",
        "/aio_image_generate/privacy/decrypt",
        "/aio_image_generate/privacy/encrypt",
        # comfyui-utils Queue Manager and Selector.
        "/api/helto_queue_manager/state",
        "/helto_selector/clear_cache",
        "/helto_selector/decrypt",
        "/helto_selector/delete_images",
        "/helto_selector/delete_mask",
        "/helto_selector/encrypt",
        "/helto_selector/migrate_masks",
        "/helto_selector/paste_image",
        "/helto_selector/register_roots",
        "/helto_selector/save_mask",
        "/helto_selector/scan_folders",
        # comfyui-utils Prompt Enhancer configuration and model acquisition.
        "/helto_prompt_enhancer/models",
        "/helto_prompt_enhancer/providers/download",
        "/helto_prompt_enhancer/providers/models",
        "/helto_prompt_enhancer/providers/settings",
        "/helto_prompt_enhancer/system_prompt",
        "/helto_prompt_enhancer/system_prompt/reset",
        "/helto_prompt_enhancer/system_prompts",
        "/helto_prompt_enhancer/system_prompts/default",
        "/helto_prompt_enhancer/system_prompts/delete",
        "/helto_prompt_enhancer/system_prompts/reset_default",
        # comfyui-utils Load Video configuration.
        "/api/helto_load_video/folders",
        "/api/helto_load_video/refresh",
        # Director storage, library, media, and metadata operations.
        "/helto_director/global_settings",
        "/helto_director/library/assets",
        "/helto_director/library/characters",
        "/helto_director/library/items",
        "/helto_director/library/projects",
        "/helto_director/media/cache/clear",
        "/helto_director/media_browser/project_takes",
        "/helto_director/media_browser/project_takes/delete",
        "/helto_director/prompt_optimizer/settings",
        "/api/helto_director/api/loras/info",
        # AIO prompt-library and LoRA metadata operations.
        "/aio_image_generate/ideogram4_prompt_library/prompts",
        "/api/aio-image-gen/api/loras/info",
    }
)
SAFE_CUSTOM_MUTATION_ROUTES = frozenset(
    {
        ("DELETE", "/api/helto_load_video/folders"),
        ("PUT", "/helto_director/library/privacy"),
    }
)
SAFE_CUSTOM_MUTATION_PATTERNS = (
    (
        frozenset({"PUT", "PATCH", "DELETE"}),
        re.compile(r"^/helto_director/library/(?:projects|characters|assets)/[^/]+$"),
    ),
    (
        frozenset({"POST"}),
        re.compile(
            r"^/helto_director/library/(?:projects|characters|assets)/[^/]+/"
            r"(?:duplicate|use|preview)$"
        ),
    ),
    (
        frozenset({"POST", "DELETE"}),
        re.compile(r"^/helto_director/media_browser/(?:image|video|audio)/folders$"),
    ),
    (
        frozenset({"PUT", "PATCH", "DELETE"}),
        re.compile(r"^/aio_image_generate/ideogram4_prompt_library/prompts/[^/]+$"),
    ),
    (
        frozenset({"POST"}),
        re.compile(
            r"^/aio_image_generate/ideogram4_prompt_library/prompts/[^/]+/"
            r"(?:duplicate|use)$"
        ),
    ),
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


def _is_safe_core_mutation(method: str, path: str) -> bool:
    return method in MUTATING_METHODS and (
        path in SAFE_CORE_MUTATION_PATHS or path.startswith(SAFE_CORE_MUTATION_PREFIXES)
    )


def _is_safe_custom_mutation(method: str, path: str) -> bool:
    if method == "POST" and path in SAFE_CUSTOM_POST_PATHS:
        return True
    if (method, path) in SAFE_CUSTOM_MUTATION_ROUTES:
        return True
    return any(
        method in methods and pattern.fullmatch(path)
        for methods, pattern in SAFE_CUSTOM_MUTATION_PATTERNS
    )


def classify_comfy_route(method: str, path: str) -> ComfyRouteKind:
    normalized_method = method.upper()
    route = (normalized_method, path)
    if normalized_method in {"GET", "HEAD", "OPTIONS"}:
        return ComfyRouteKind.PASSTHROUGH
    if route in WORKFLOW_ROUTES:
        return ComfyRouteKind.WORKFLOW
    if route in CONTROL_ROUTES or (normalized_method == "POST" and _is_job_cancel(path)):
        return ComfyRouteKind.CONTROL
    if route in LIFECYCLE_ROUTES:
        return ComfyRouteKind.LIFECYCLE
    if route in BLOCKED_GPU_ROUTES:
        return ComfyRouteKind.BLOCKED_GPU
    if _is_safe_core_mutation(normalized_method, path) or _is_safe_custom_mutation(
        normalized_method, path
    ):
        return ComfyRouteKind.PASSTHROUGH
    return ComfyRouteKind.UNKNOWN_MUTATION
