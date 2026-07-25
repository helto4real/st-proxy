from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiohttp import ClientSession

from ..errors import ConfigurationError
from .base import BackendTimeouts, LlmBackend
from .koboldcpp import KoboldCppBackend
from .ollama import OllamaBackend

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
        timeouts=_timeouts(config),
    )


def _build_ollama(session: ClientSession, config: BrokerConfig) -> OllamaBackend:
    return OllamaBackend(
        session,
        origin=config.llm_url,
        timeouts=_timeouts(config),
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
}


def available_backend_kinds() -> tuple[str, ...]:
    return tuple(_BACKENDS)


def _backend_spec(kind: str) -> BackendSpec:
    try:
        return _BACKENDS[kind]
    except KeyError as exc:
        supported = ", ".join(available_backend_kinds())
        raise ConfigurationError(
            f"unsupported LLM backend {kind!r}; choose one of: {supported}"
        ) from exc


def backend_default_origin(kind: str) -> str:
    return _backend_spec(kind).default_origin


def build_llm_backend(
    session: ClientSession,
    config: BrokerConfig,
) -> LlmBackend[Any]:
    return _backend_spec(config.llm_backend).build(session, config)
