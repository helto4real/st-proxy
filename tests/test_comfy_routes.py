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
    assert classify_comfy_route("POST", "/interrupt") is ComfyRouteKind.CONTROL
    assert classify_comfy_route("POST", "/queue") is ComfyRouteKind.CONTROL
    assert (
        classify_comfy_route("POST", "/api/jobs/example/cancel")
        is ComfyRouteKind.CONTROL
    )
    assert classify_comfy_route("POST", "/free") is ComfyRouteKind.LIFECYCLE


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
