from __future__ import annotations

import os
from unittest.mock import patch

from st_proxy.cli import build_parser, config_from_args


def _parse(*arguments: str):
    with patch.dict(os.environ, {}, clear=True):
        return config_from_args(build_parser().parse_args(arguments))


def test_default_cli_uses_koboldcpp_compatibility_defaults() -> None:
    config = _parse()

    assert config.llm_backend == "koboldcpp"
    assert config.llm_url == "http://127.0.0.1:5002"
    assert config.idle_timeout == 60
    assert config.allow_unknown_comfy_routes


def test_ollama_cli_uses_backend_specific_default_origin() -> None:
    config = _parse("--llm-backend", "ollama")

    assert config.llm_backend == "ollama"
    assert config.llm_url == "http://127.0.0.1:11434"


def test_legacy_kobold_url_flag_maps_to_generic_llm_origin() -> None:
    config = _parse("--kobold-url", "http://127.0.0.1:6000")

    assert config.llm_backend == "koboldcpp"
    assert config.llm_url == "http://127.0.0.1:6000"


def test_generic_llm_url_takes_selected_backend() -> None:
    config = _parse(
        "--llm-backend",
        "ollama",
        "--llm-url",
        "http://127.0.0.1:12000",
    )

    assert config.llm_backend == "ollama"
    assert config.llm_url == "http://127.0.0.1:12000"


def test_idle_timeout_cli_option_maps_to_config() -> None:
    config = _parse("--idle-timeout", "12.5")

    assert config.idle_timeout == 12.5


def test_strict_comfy_route_policy_is_explicit() -> None:
    config = _parse("--strict-comfy-routes")

    assert not config.allow_unknown_comfy_routes


def test_legacy_allow_unknown_comfy_route_flag_keeps_transparent_policy() -> None:
    config = _parse("--allow-unknown-comfy-routes")

    assert config.allow_unknown_comfy_routes


def test_strict_comfy_route_environment_setting_is_supported() -> None:
    with patch.dict(os.environ, {"ST_PROXY_STRICT_COMFY_ROUTES": "true"}, clear=True):
        config = config_from_args(build_parser().parse_args(()))

    assert not config.allow_unknown_comfy_routes
