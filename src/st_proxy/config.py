from __future__ import annotations

import ipaddress
import os
from dataclasses import InitVar, dataclass, field, replace
from urllib.parse import urlsplit, urlunsplit

from .errors import ConfigurationError

CONVENTIONAL_TEST_UNSAFE_PORTS = frozenset({5001, 5002, 8188, 8189})


def normalize_origin(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.port is None:
        raise ConfigurationError("upstream URLs must include http(s), a host, and an explicit port")
    if parsed.username or parsed.password:
        raise ConfigurationError("credentials are not allowed in upstream URLs")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class TestEndpointRegistry:
    """Process-local allowlist for disposable test servers.

    Test code creates this object, starts a server on port 0, and then registers the
    resulting loopback origin. There is deliberately no CLI or environment mechanism
    for populating it.
    """

    __test__ = False

    def __init__(self) -> None:
        self._pid = os.getpid()
        self._origins: set[str] = set()

    def approve_disposable(self, origin: str) -> str:
        normalized = normalize_origin(origin)
        parsed = urlsplit(normalized)
        if self._pid != os.getpid():
            raise ConfigurationError("test endpoint registry cannot cross process boundaries")
        if not _is_loopback_host(parsed.hostname or ""):
            raise ConfigurationError("test fixtures must use a loopback address")
        if parsed.port in CONVENTIONAL_TEST_UNSAFE_PORTS:
            raise ConfigurationError("test fixtures must use dynamically allocated ports")
        self._origins.add(normalized)
        return normalized

    def contains(self, origin: str) -> bool:
        return self._pid == os.getpid() and normalize_origin(origin) in self._origins


@dataclass(frozen=True, slots=True)
class BrokerConfig:
    listen_host: str = "127.0.0.1"
    chat_port: int = 5001
    image_port: int = 8188
    llm_backend: str = "koboldcpp"
    llm_url: str = ""
    kobold_url: InitVar[str | None] = None
    comfy_url: str = "http://127.0.0.1:8189"
    kobold_admin_password: str | None = None
    request_timeout: float = 600.0
    image_timeout: float = 1800.0
    chat_drain_timeout: float = 1800.0
    unload_timeout: float = 180.0
    reload_timeout: float = 600.0
    cleanup_timeout: float = 60.0
    idle_timeout: float = 60.0
    poll_interval: float = 0.5
    allow_unknown_comfy_routes: bool = True
    test_mode: bool = False
    test_registry: TestEndpointRegistry | None = field(default=None, repr=False, compare=False)

    def __post_init__(self, kobold_url: str | None) -> None:
        if not self.llm_backend:
            raise ConfigurationError("llm_backend cannot be empty")
        from .llm import backend_default_origin

        if kobold_url and self.llm_url:
            if normalize_origin(kobold_url) != normalize_origin(self.llm_url):
                raise ConfigurationError("llm_url and kobold_url cannot disagree")
        llm_url = kobold_url or self.llm_url or backend_default_origin(self.llm_backend)
        object.__setattr__(self, "llm_url", normalize_origin(llm_url))
        object.__setattr__(self, "comfy_url", normalize_origin(self.comfy_url))
        if not 0 <= self.chat_port <= 65535 or not 0 <= self.image_port <= 65535:
            raise ConfigurationError("listen ports must be between 0 and 65535")
        if self.listen_host == "0.0.0.0" or self.listen_host == "::":
            # Non-loopback is supported when explicitly requested, but the default stays safe.
            pass
        for name in (
            "request_timeout",
            "image_timeout",
            "chat_drain_timeout",
            "unload_timeout",
            "reload_timeout",
            "cleanup_timeout",
            "idle_timeout",
            "poll_interval",
        ):
            if getattr(self, name) <= 0:
                raise ConfigurationError(f"{name} must be greater than zero")
        if self.test_mode:
            if self.chat_port != 0 or self.image_port != 0:
                raise ConfigurationError("test mode requires dynamically allocated broker ports")
            if self.test_registry is None:
                raise ConfigurationError("test mode requires a process-local endpoint registry")
            for origin in (self.llm_url, self.comfy_url):
                if not self.test_registry.contains(origin):
                    raise ConfigurationError("test mode rejected an unregistered upstream endpoint")

    @classmethod
    def for_test(
        cls,
        *,
        kobold_url: str | None = None,
        llm_url: str | None = None,
        llm_backend: str = "koboldcpp",
        comfy_url: str,
        registry: TestEndpointRegistry,
        **overrides: object,
    ) -> BrokerConfig:
        selected_llm_url = llm_url or kobold_url
        if selected_llm_url is None:
            raise ConfigurationError("for_test requires llm_url")
        config = cls(
            chat_port=0,
            image_port=0,
            llm_backend=llm_backend,
            llm_url=selected_llm_url,
            comfy_url=comfy_url,
            test_mode=True,
            test_registry=registry,
            request_timeout=5,
            image_timeout=5,
            chat_drain_timeout=5,
            unload_timeout=5,
            reload_timeout=5,
            cleanup_timeout=5,
            idle_timeout=60,
            poll_interval=0.01,
        )
        return replace(config, **overrides) if overrides else config
