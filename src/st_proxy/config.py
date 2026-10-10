from __future__ import annotations

import ipaddress
import os
from dataclasses import InitVar, dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .errors import ConfigurationError

CONVENTIONAL_TEST_UNSAFE_PORTS = frozenset({
    5001, 5002, 5003, 5006, 8188, 8189, 48640, 48642, 48644, 48646,
})
DEFAULT_MCP_WORKFLOWS_DIR = str(Path(__file__).resolve().parents[2] / "workflows")


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
    mcp_port: int | None = None
    mcp_workflows_dir: str = DEFAULT_MCP_WORKFLOWS_DIR
    mcp_output_dir: str | None = None
    mcp_input_dir: str = "/run/media/veracrypt1/mcp-inputs"
    mcp_token: str | None = field(default=None, repr=False)
    model_profiles: str | None = None
    llm_backend: str = "koboldcpp"
    llm_url: str = ""
    tabby_url: str = "http://127.0.0.1:5003"
    prism_url: str = "http://127.0.0.1:5006"
    sc117_url: str = "http://127.0.0.1:48640"
    orca_url: str = "http://127.0.0.1:48642"
    sc117_128k_url: str = "http://127.0.0.1:48644"
    orca_128k_url: str = "http://127.0.0.1:48646"
    kobold_url: InitVar[str | None] = None
    comfy_url: str = "http://127.0.0.1:8189"
    kobold_admin_password: str | None = None
    kobold_router_mode: bool = False
    kobold_model_cache: str | None = None
    kobold_default_model: str = "initial_model"
    prism_model: str = "prism-bonsai-2-27b-abliterated"
    tabby_model: str | None = None
    tabby_max_seq_len: int = 32768
    max_chat_body_bytes: int = 32 * 1024**2
    connect_timeout: float = 30.0
    request_timeout: float = 3900.0
    image_timeout: float = 1800.0
    chat_drain_timeout: float = 3900.0
    unload_timeout: float = 180.0
    reload_timeout: float = 600.0
    cleanup_timeout: float = 60.0
    idle_timeout: float = 60.0
    restore_llm_on_idle: bool = False
    poll_interval: float = 0.5
    comfy_poll_failure_limit: int = 6
    max_workflow_body_bytes: int = 64 * 1024**2
    max_queued_images: int = 32
    max_queued_workflow_bytes: int = 256 * 1024**2
    allow_unknown_comfy_routes: bool = True
    test_mode: bool = False
    test_registry: TestEndpointRegistry | None = field(default=None, repr=False, compare=False)

    def __post_init__(self, kobold_url: str | None) -> None:
        if self.mcp_port is not None:
            if not 0 <= self.mcp_port <= 65535:
                raise ConfigurationError("mcp_port must be between 0 and 65535")
            if not self.mcp_workflows_dir or not self.mcp_output_dir:
                raise ConfigurationError("MCP requires workflow/output directories")
            if self.test_mode and self.mcp_port != 0:
                raise ConfigurationError("test mode requires a dynamically allocated MCP port")
        if not self.kobold_default_model.strip():
            raise ConfigurationError("kobold_default_model cannot be empty")
        if not self.prism_model.strip():
            raise ConfigurationError("prism_model cannot be empty")
        if not self.llm_backend:
            raise ConfigurationError("llm_backend cannot be empty")
        if self.kobold_router_mode and self.llm_backend != "koboldcpp":
            raise ConfigurationError("KoboldCpp Router mode requires the koboldcpp backend")
        from .llm import backend_default_origin

        if kobold_url and self.llm_url:
            if normalize_origin(kobold_url) != normalize_origin(self.llm_url):
                raise ConfigurationError("llm_url and kobold_url cannot disagree")
        llm_url = kobold_url or self.llm_url or backend_default_origin(self.llm_backend)
        object.__setattr__(self, "llm_url", normalize_origin(llm_url))
        for name in ("tabby_url", "prism_url", "sc117_url", "orca_url",
                     "sc117_128k_url", "orca_128k_url"):
            object.__setattr__(self, name, normalize_origin(getattr(self, name)))
        if self.llm_backend == "all" and len({
            self.llm_url, self.tabby_url, self.prism_url, self.sc117_url, self.orca_url,
            self.sc117_128k_url, self.orca_128k_url,
        }) != 7:
            raise ConfigurationError("all backends require distinct upstream origins")
        object.__setattr__(self, "comfy_url", normalize_origin(self.comfy_url))
        if not 0 <= self.chat_port <= 65535 or not 0 <= self.image_port <= 65535:
            raise ConfigurationError("listen ports must be between 0 and 65535")
        if self.listen_host == "0.0.0.0" or self.listen_host == "::":
            # Non-loopback is supported when explicitly requested, but the default stays safe.
            pass
        for name in (
            "connect_timeout",
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
        for name in (
            "tabby_max_seq_len",
            "max_chat_body_bytes",
            "comfy_poll_failure_limit",
            "max_workflow_body_bytes",
            "max_queued_images",
            "max_queued_workflow_bytes",
        ):
            if getattr(self, name) <= 0:
                raise ConfigurationError(f"{name} must be greater than zero")
        if self.max_queued_workflow_bytes < self.max_workflow_body_bytes:
            raise ConfigurationError(
                "max_queued_workflow_bytes must be at least max_workflow_body_bytes"
            )
        if self.test_mode:
            if self.chat_port != 0 or self.image_port != 0:
                raise ConfigurationError("test mode requires dynamically allocated broker ports")
            if self.test_registry is None:
                raise ConfigurationError("test mode requires a process-local endpoint registry")
            origins = [self.llm_url, self.comfy_url]
            if self.llm_backend == "all":
                origins.extend((self.tabby_url, self.prism_url, self.sc117_url, self.orca_url,
                                self.sc117_128k_url, self.orca_128k_url))
            for origin in origins:
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
        tabby_url: str = "http://127.0.0.1:5003",
        prism_url: str = "http://127.0.0.1:5006",
        sc117_url: str = "http://127.0.0.1:48640",
        orca_url: str = "http://127.0.0.1:48642",
        sc117_128k_url: str = "http://127.0.0.1:48644",
        orca_128k_url: str = "http://127.0.0.1:48646",
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
            tabby_url=tabby_url,
            prism_url=prism_url,
            sc117_url=sc117_url,
            orca_url=orca_url,
            sc117_128k_url=sc117_128k_url,
            orca_128k_url=orca_128k_url,
            comfy_url=comfy_url,
            test_mode=True,
            test_registry=registry,
            connect_timeout=5,
            request_timeout=5,
            image_timeout=5,
            chat_drain_timeout=5,
            unload_timeout=5,
            reload_timeout=5,
            cleanup_timeout=10,  # Includes the five-second cleanup settling period.
            idle_timeout=60,
            poll_interval=0.01,
            comfy_poll_failure_limit=3,
        )
        return replace(config, **overrides) if overrides else config
