from aiohttp.test_utils import make_mocked_request

from st_proxy.http import upstream_url


def test_upstream_url_preserves_raw_path_and_query_encoding() -> None:
    request = make_mocked_request(
        "GET",
        "/api/userdata/workflows%2Fsynthetic.json?label=synthetic%20workflow",
    )

    assert upstream_url("http://127.0.0.1:8188", request) == (
        "http://127.0.0.1:8188/api/userdata/workflows%2Fsynthetic.json?label=synthetic%20workflow"
    )
    assert upstream_url(
        "http://127.0.0.1:8188",
        request,
        raw_path="/userdata/workflows%2Fsynthetic.json",
    ) == ("http://127.0.0.1:8188/userdata/workflows%2Fsynthetic.json?label=synthetic%20workflow")
