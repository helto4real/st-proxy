from st_proxy.comfy_routes import (
    ComfyRouteKind,
    classify_comfy_route,
    comfy_upstream_path,
)


def test_known_frontend_api_aliases_use_legacy_upstream_routes() -> None:
    assert comfy_upstream_path("/api/userdata") == "/userdata"
    assert (
        comfy_upstream_path("/api/userdata/workflows/synthetic.json")
        == "/userdata/workflows/synthetic.json"
    )
    assert comfy_upstream_path("/api/settings/theme") == "/settings/theme"
    assert comfy_upstream_path("/api/users") == "/users"
    assert comfy_upstream_path("/api/prompt") == "/prompt"
    assert comfy_upstream_path("/api/interrupt") == "/interrupt"
    assert comfy_upstream_path("/api/queue") == "/queue"
    assert comfy_upstream_path("/api/free") == "/free"


def test_genuine_api_routes_and_partial_prefixes_are_not_rewritten() -> None:
    assert comfy_upstream_path("/api/jobs/example") == "/api/jobs/example"
    assert comfy_upstream_path("/api/assets/example") == "/api/assets/example"
    assert comfy_upstream_path("/api/prompt/example") == "/api/prompt/example"
    assert comfy_upstream_path("/api/users/example") == "/api/users/example"
    assert comfy_upstream_path("/api/userdatabase") == "/api/userdatabase"


def test_read_only_and_websocket_routes_pass_through() -> None:
    assert classify_comfy_route("GET", "/") is ComfyRouteKind.PASSTHROUGH
    assert classify_comfy_route("GET", "/ws") is ComfyRouteKind.PASSTHROUGH
    assert classify_comfy_route("HEAD", "/view") is ComfyRouteKind.PASSTHROUGH


def test_local_and_cloud_workflow_routes_are_coordinated() -> None:
    assert classify_comfy_route("POST", "/prompt") is ComfyRouteKind.WORKFLOW
    assert classify_comfy_route("POST", "/api/prompt") is ComfyRouteKind.WORKFLOW


def test_control_and_lifecycle_routes_are_distinct() -> None:
    control_paths = (
        "/interrupt",
        "/api/interrupt",
        "/queue",
        "/api/queue",
        "/api/helto_save_image_advanced/release",
        "/api/helto_save_video_advanced/release",
        "/helto_prompt_enhancer/providers/unload",
        "/helto_director/prompt_optimizer/models/unload",
    )
    for path in control_paths:
        assert classify_comfy_route("POST", path) is ComfyRouteKind.CONTROL
    assert (
        classify_comfy_route("POST", "/api/jobs/example/cancel")
        is ComfyRouteKind.CONTROL
    )
    assert classify_comfy_route("POST", "/free") is ComfyRouteKind.LIFECYCLE
    assert classify_comfy_route("POST", "/api/free") is ComfyRouteKind.LIFECYCLE


def test_known_ui_mutations_pass_but_unknown_custom_mutations_fail_closed() -> None:
    assert classify_comfy_route("POST", "/upload/image") is ComfyRouteKind.PASSTHROUGH
    assert classify_comfy_route("POST", "/api/users") is ComfyRouteKind.PASSTHROUGH
    assert classify_comfy_route("PATCH", "/userdata/default/workflows/a.json") is (
        ComfyRouteKind.PASSTHROUGH
    )
    assert classify_comfy_route("POST", "/custom-node/run-model") is (
        ComfyRouteKind.UNKNOWN_MUTATION
    )


def test_known_helto_privacy_mutations_pass_but_namespace_stays_fail_closed() -> None:
    safe_paths = (
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
    )

    for path in safe_paths:
        assert classify_comfy_route("POST", path) is ComfyRouteKind.PASSTHROUGH

    assert classify_comfy_route("POST", "/helto_director/privacy/run-model") is (
        ComfyRouteKind.UNKNOWN_MUTATION
    )
    assert classify_comfy_route("POST", "/helto_privacy/encrypt") is (
        ComfyRouteKind.UNKNOWN_MUTATION
    )
    assert classify_comfy_route("POST", "/api/helto_director/h3_preview/render") is (
        ComfyRouteKind.UNKNOWN_MUTATION
    )


def test_reviewed_comfyui_utils_mutations_pass_through() -> None:
    post_paths = (
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
        "/api/helto_load_video/folders",
        "/api/helto_load_video/refresh",
    )

    for path in post_paths:
        assert classify_comfy_route("POST", path) is ComfyRouteKind.PASSTHROUGH
    assert classify_comfy_route("DELETE", "/api/helto_load_video/folders") is (
        ComfyRouteKind.PASSTHROUGH
    )


def test_reviewed_director_mutations_pass_through() -> None:
    post_paths = (
        "/helto_director/global_settings",
        "/helto_director/library/items",
        "/helto_director/library/projects",
        "/helto_director/library/characters",
        "/helto_director/library/assets",
        "/helto_director/media/cache/clear",
        "/helto_director/media_browser/project_takes",
        "/helto_director/media_browser/project_takes/delete",
        "/helto_director/prompt_optimizer/settings",
        "/api/helto_director/api/loras/info",
    )

    for path in post_paths:
        assert classify_comfy_route("POST", path) is ComfyRouteKind.PASSTHROUGH

    assert classify_comfy_route("PUT", "/helto_director/library/privacy") is (
        ComfyRouteKind.PASSTHROUGH
    )
    for method in ("PUT", "PATCH", "DELETE"):
        assert classify_comfy_route(
            method, "/helto_director/library/projects/synthetic-id"
        ) is ComfyRouteKind.PASSTHROUGH
    for action in ("duplicate", "use", "preview"):
        assert classify_comfy_route(
            "POST", f"/helto_director/library/assets/synthetic-id/{action}"
        ) is ComfyRouteKind.PASSTHROUGH
    for method in ("POST", "DELETE"):
        assert classify_comfy_route(
            method, "/helto_director/media_browser/video/folders"
        ) is ComfyRouteKind.PASSTHROUGH


def test_reviewed_smartprompt_and_aio_mutations_pass_through() -> None:
    post_paths = (
        "/helto_spm/privacy/decrypt",
        "/helto_spm/privacy/encrypt",
        "/aio_image_generate/privacy/decrypt",
        "/aio_image_generate/privacy/encrypt",
        "/aio_image_generate/ideogram4_prompt_library/prompts",
        "/api/aio-image-gen/api/loras/info",
    )

    for path in post_paths:
        assert classify_comfy_route("POST", path) is ComfyRouteKind.PASSTHROUGH
    for method in ("PUT", "PATCH", "DELETE"):
        assert classify_comfy_route(
            method,
            "/aio_image_generate/ideogram4_prompt_library/prompts/synthetic-id",
        ) is ComfyRouteKind.PASSTHROUGH
    for action in ("duplicate", "use"):
        assert classify_comfy_route(
            "POST",
            f"/aio_image_generate/ideogram4_prompt_library/prompts/synthetic-id/{action}",
        ) is ComfyRouteKind.PASSTHROUGH


def test_reviewed_custom_rules_remain_method_and_path_scoped() -> None:
    unknown_routes = (
        ("DELETE", "/helto_spm/privacy/encrypt"),
        ("POST", "/helto_director/library/projects/synthetic-id/run-model"),
        ("POST", "/helto_director/media_browser/model/folders"),
        ("POST", "/aio_image_generate/ideogram4_prompt_library/prompts/id/render"),
        ("POST", "/helto_director/prompt_optimizer/optimize"),
        ("POST", "/helto_director/prompt_optimizer/optimize/start"),
    )

    for method, path in unknown_routes:
        assert classify_comfy_route(method, path) is ComfyRouteKind.UNKNOWN_MUTATION
