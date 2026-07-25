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
