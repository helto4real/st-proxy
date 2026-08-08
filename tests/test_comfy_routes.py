from st_proxy.comfy_routes import ComfyRouteKind, classify_comfy_route


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
    assert classify_comfy_route("PATCH", "/userdata/default/workflows/a.json") is (
        ComfyRouteKind.PASSTHROUGH
    )
    assert classify_comfy_route("POST", "/custom-node/run-model") is (
        ComfyRouteKind.UNKNOWN_MUTATION
    )
