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
    assert config.connect_timeout == 30
    assert config.request_timeout == 3900
    assert config.chat_drain_timeout == 3900
    assert config.idle_timeout == 60
    assert not config.restore_llm_on_idle
    assert config.comfy_poll_failure_limit == 6
    assert config.max_workflow_body_bytes == 64 * 1024**2
    assert config.max_queued_images == 32
    assert config.max_queued_workflow_bytes == 256 * 1024**2
    assert config.allow_unknown_comfy_routes
    assert not config.kobold_router_mode
    assert config.max_chat_body_bytes == 32 * 1024**2


def test_router_options_and_environment_map_to_config() -> None:
    config = _parse("--kobold-router-mode", "--kobold-model-cache", "/tmp/models.json",
                    "--max-chat-body-bytes", "4096")
    assert config.kobold_router_mode
    assert config.kobold_model_cache == "/tmp/models.json"
    assert config.max_chat_body_bytes == 4096
    with patch.dict(os.environ, {"ST_PROXY_KOBOLD_ROUTER_MODE": "true"}, clear=True):
        assert config_from_args(build_parser().parse_args(())).kobold_router_mode


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


def test_restore_llm_on_idle_is_explicitly_enabled() -> None:
    config = _parse("--restore-llm-on-idle")

    assert config.restore_llm_on_idle


def test_restore_llm_on_idle_environment_setting_is_supported() -> None:
    with patch.dict(
        os.environ,
        {"ST_PROXY_RESTORE_LLM_ON_IDLE": "true"},
        clear=True,
    ):
        config = config_from_args(build_parser().parse_args(()))

    assert config.restore_llm_on_idle


def test_stability_limit_options_map_to_config() -> None:
    config = _parse(
        "--connect-timeout",
        "4.5",
        "--comfy-poll-failure-limit",
        "4",
        "--max-workflow-body-bytes",
        "100",
        "--max-queued-images",
        "5",
        "--max-queued-workflow-bytes",
        "500",
    )

    assert config.connect_timeout == 4.5
    assert config.comfy_poll_failure_limit == 4
    assert config.max_workflow_body_bytes == 100
    assert config.max_queued_images == 5
    assert config.max_queued_workflow_bytes == 500


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


def test_tabby_cli_and_environment_settings() -> None:
    config = _parse("--llm-backend", "tabbyapi", "--tabby-model", "synthetic-exl3")
    assert config.llm_url == "http://127.0.0.1:5003"
    assert config.tabby_model == "synthetic-exl3"
    assert config.tabby_max_seq_len == 32768
    with patch.dict(os.environ, {
        "ST_PROXY_LLM_BACKEND": "tabbyapi",
        "ST_PROXY_TABBY_MODEL": "synthetic-other",
        "ST_PROXY_TABBY_MAX_SEQ_LEN": "16384",
    }, clear=True):
        config = config_from_args(build_parser().parse_args(()))
    assert config.tabby_model == "synthetic-other"
    assert config.tabby_max_seq_len == 16384
