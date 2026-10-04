from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from aiohttp import ClientSession

from ..errors import ConfigurationError
from .base import BackendTimeouts, LlmBackend
from .koboldcpp import KoboldCppBackend
from .multi import MultiBackend
from .ollama import OllamaBackend
from .prism import PrismBackend
from .strata import StrataBackend
from .tabbyapi import TabbyApiBackend

if TYPE_CHECKING:
    from ..config import BrokerConfig


@dataclass(frozen=True, slots=True)
class BackendSpec:
    kind: str
    label: str
    default_origin: str
    build: Callable[[ClientSession, BrokerConfig], LlmBackend[Any]]


def _timeouts(config: BrokerConfig) -> BackendTimeouts:
    return BackendTimeouts(
        request=config.request_timeout,
        release=config.unload_timeout,
        acquire=config.reload_timeout,
        poll_interval=config.poll_interval,
    )


def _build_koboldcpp(
    session: ClientSession,
    config: BrokerConfig,
) -> KoboldCppBackend:
    return KoboldCppBackend(
        session,
        origin=config.llm_url,
        admin_password=config.kobold_admin_password,
        router_mode=config.kobold_router_mode,
        timeouts=_timeouts(config),
    )


def _build_ollama(session: ClientSession, config: BrokerConfig) -> OllamaBackend:
    return OllamaBackend(
        session,
        origin=config.llm_url,
        timeouts=_timeouts(config),
    )


def _build_tabbyapi(session: ClientSession, config: BrokerConfig) -> TabbyApiBackend:
    return TabbyApiBackend(
        session,
        origin=config.llm_url,
        model=config.tabby_model,
        max_seq_len=config.tabby_max_seq_len,
        timeouts=_timeouts(config),
    )


def _build_prism(session: ClientSession, config: BrokerConfig) -> PrismBackend:
    return PrismBackend(
        session, origin=config.llm_url, model=config.prism_model, timeouts=_timeouts(config),
    )


def _build_strata(session: ClientSession, config: BrokerConfig) -> StrataBackend:
    return StrataBackend(
        session, kind=config.llm_backend, label=_backend_spec(config.llm_backend).label,
        origin=config.llm_url, timeouts=_timeouts(config),
    )


_BACKENDS = {
    "koboldcpp": BackendSpec(
        "koboldcpp",
        "KoboldCpp",
        "http://127.0.0.1:5002",
        _build_koboldcpp,
    ),
    "ollama": BackendSpec(
        "ollama",
        "Ollama",
        "http://127.0.0.1:11434",
        _build_ollama,
    ),
    "sc117": BackendSpec("sc117", "Qwen SC117", "http://127.0.0.1:48640", _build_strata),
    "orca": BackendSpec("orca", "Qwen Orca", "http://127.0.0.1:48642", _build_strata),
    "prism": BackendSpec("prism", "Prism", "http://127.0.0.1:5006", _build_prism),
    "tabbyapi": BackendSpec(
        "tabbyapi", "TabbyAPI", "http://127.0.0.1:5003", _build_tabbyapi,
    ),
}


def available_backend_kinds() -> tuple[str, ...]:
    return (*_BACKENDS, "all")


def _backend_spec(kind: str) -> BackendSpec:
    try:
        return _BACKENDS[kind]
    except KeyError as exc:
        supported = ", ".join(available_backend_kinds())
        raise ConfigurationError(
            f"unsupported LLM backend {kind!r}; choose one of: {supported}"
        ) from exc


def backend_default_origin(kind: str) -> str:
    if kind == "all":
        return _BACKENDS["koboldcpp"].default_origin
    return _backend_spec(kind).default_origin


def build_llm_backend(
    session: ClientSession,
    config: BrokerConfig,
) -> LlmBackend[Any]:
    if config.llm_backend == "all":
        return MultiBackend({
            "koboldcpp": _build_koboldcpp(session, replace(
                config, llm_backend="koboldcpp", kobold_router_mode=True)),
            "tabbyapi": _build_tabbyapi(session, replace(
                config, llm_backend="tabbyapi", llm_url=config.tabby_url)),
            "sc117": _build_strata(session, replace(
                config, llm_backend="sc117", llm_url=config.sc117_url)),
            "orca": _build_strata(session, replace(
                config, llm_backend="orca", llm_url=config.orca_url)),
            "prism": _build_prism(session, replace(
                config, llm_backend="prism", llm_url=config.prism_url)),
        })
    return _backend_spec(config.llm_backend).build(session, config)
