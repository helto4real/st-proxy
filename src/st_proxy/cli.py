from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from collections.abc import Sequence
from dataclasses import replace

from aiohttp import ClientSession, ClientTimeout, TCPConnector

from .config import BrokerConfig
from .errors import BrokerError, ConfigurationError
from .llm import (
    available_backend_kinds,
    backend_default_origin,
    build_llm_backend,
)
from .service import BrokerService

LOG = logging.getLogger(__name__)


def _env(name: str, default: str | None = None) -> str | None:
    return os.getenv(f"ST_PROXY_{name}", default)


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    return int(value) if value is not None else default


def _env_float(name: str, default: float) -> float:
    value = _env(name)
    return float(value) if value is not None else default


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"ST_PROXY_{name} must be a boolean")


def _allow_unknown_comfy_routes_default() -> bool:
    if _env("STRICT_COMFY_ROUTES") is not None:
        return not _env_bool("STRICT_COMFY_ROUTES")
    return _env_bool("ALLOW_UNKNOWN_COMFY_ROUTES", True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local SillyTavern VRAM handoff broker")
    parser.add_argument("--listen-host", default=_env("LISTEN_HOST", "127.0.0.1"))
    parser.add_argument("--chat-port", type=int, default=_env_int("CHAT_PORT", 5001))
    parser.add_argument("--image-port", type=int, default=_env_int("IMAGE_PORT", 8188))
    parser.add_argument(
        "--llm-backend",
        choices=available_backend_kinds(),
        default=_env("LLM_BACKEND", "koboldcpp"),
    )
    parser.add_argument(
        "--llm-url",
        "--kobold-url",
        dest="llm_url",
        default=_env("LLM_URL") or _env("KOBOLD_URL"),
        help="LLM origin; --kobold-url is retained as a compatibility alias",
    )
    parser.add_argument("--comfy-url", default=_env("COMFY_URL", "http://127.0.0.1:8189"))
    parser.add_argument(
        "--kobold-admin-password",
        default=_env("KOBOLD_ADMIN_PASSWORD"),
        help="Prefer ST_PROXY_KOBOLD_ADMIN_PASSWORD to avoid shell history",
    )
    parser.add_argument("--request-timeout", type=float, default=_env_float("REQUEST_TIMEOUT", 600))
    parser.add_argument("--image-timeout", type=float, default=_env_float("IMAGE_TIMEOUT", 1800))
    parser.add_argument(
        "--chat-drain-timeout", type=float, default=_env_float("CHAT_DRAIN_TIMEOUT", 1800)
    )
    parser.add_argument("--unload-timeout", type=float, default=_env_float("UNLOAD_TIMEOUT", 180))
    parser.add_argument("--reload-timeout", type=float, default=_env_float("RELOAD_TIMEOUT", 600))
    parser.add_argument("--cleanup-timeout", type=float, default=_env_float("CLEANUP_TIMEOUT", 60))
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=_env_float("IDLE_TIMEOUT", 60),
        help="restore the selected LLM after ComfyUI has been idle for this many seconds",
    )
    parser.add_argument("--poll-interval", type=float, default=_env_float("POLL_INTERVAL", 0.5))
    route_policy = parser.add_mutually_exclusive_group()
    route_policy.add_argument(
        "--strict-comfy-routes",
        action="store_false",
        dest="allow_unknown_comfy_routes",
        default=_allow_unknown_comfy_routes_default(),
        help="reject unclassified mutating ComfyUI routes; disabled by default",
    )
    route_policy.add_argument(
        "--allow-unknown-comfy-routes",
        action="store_true",
        dest="allow_unknown_comfy_routes",
        help=(
            "compatibility alias for the default transparent ComfyUI route policy"
        ),
    )
    parser.add_argument(
        "--check-backend",
        action="store_true",
        help="validate the configured LLM lifecycle API and exit",
    )
    parser.add_argument(
        "--backend-check-timeout",
        type=float,
        default=_env_float("BACKEND_CHECK_TIMEOUT", 2),
    )
    parser.add_argument("--log-level", default=_env("LOG_LEVEL", "INFO"))
    return parser


def config_from_args(args: argparse.Namespace) -> BrokerConfig:
    if (_env("TEST_MODE", "")).lower() in {"1", "true", "yes", "on"}:
        raise ConfigurationError("test mode is available only to the in-process test harness")
    llm_url = args.llm_url or backend_default_origin(args.llm_backend)
    return BrokerConfig(
        listen_host=args.listen_host,
        chat_port=args.chat_port,
        image_port=args.image_port,
        llm_backend=args.llm_backend,
        llm_url=llm_url,
        comfy_url=args.comfy_url,
        kobold_admin_password=args.kobold_admin_password,
        request_timeout=args.request_timeout,
        image_timeout=args.image_timeout,
        chat_drain_timeout=args.chat_drain_timeout,
        unload_timeout=args.unload_timeout,
        reload_timeout=args.reload_timeout,
        cleanup_timeout=args.cleanup_timeout,
        idle_timeout=args.idle_timeout,
        poll_interval=args.poll_interval,
        allow_unknown_comfy_routes=args.allow_unknown_comfy_routes,
    )


async def check_backend(config: BrokerConfig, timeout_seconds: float) -> None:
    if timeout_seconds <= 0:
        raise ConfigurationError("backend_check_timeout must be greater than zero")
    check_config = replace(
        config,
        request_timeout=min(config.request_timeout, timeout_seconds),
        reload_timeout=min(config.reload_timeout, timeout_seconds),
        unload_timeout=min(config.unload_timeout, timeout_seconds),
        poll_interval=min(config.poll_interval, timeout_seconds),
    )
    timeout = ClientTimeout(total=None, sock_connect=timeout_seconds)
    async with ClientSession(
        timeout=timeout,
        connector=TCPConnector(force_close=True),
        auto_decompress=False,
    ) as session:
        backend = build_llm_backend(session, check_config)
        await backend.validate_control()
        await backend.snapshot_ready()
        LOG.info("LLM backend ready: backend=%s", backend.info.label)


async def run(config: BrokerConfig) -> None:
    service = BrokerService(config)
    await service.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signame, stop.set)
        except (NotImplementedError, RuntimeError):
            signal.signal(signame, lambda *_args: loop.call_soon_threadsafe(stop.set))
    assert service.coordinator is not None
    status = service.coordinator.status()
    if status["chat_available"]:
        LOG.info(
            "broker ready: chat=http://%s:%s image=http://%s:%s state=%s",
            config.listen_host,
            service.chat_port,
            config.listen_host,
            service.image_port,
            status["state"],
        )
    else:
        LOG.error(
            "broker listening but unavailable: chat=http://%s:%s image=http://%s:%s "
            "state=%s error=%s",
            config.listen_host,
            service.chat_port,
            config.listen_host,
            service.image_port,
            status["state"],
            status["last_error"],
        )
    try:
        await stop.wait()
    finally:
        await service.stop()


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = config_from_args(args)
        if args.check_backend:
            asyncio.run(check_backend(config, args.backend_check_timeout))
        else:
            asyncio.run(run(config))
    except (ConfigurationError, ValueError) as exc:
        parser.error(str(exc))
    except BrokerError as exc:
        LOG.error("%s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
