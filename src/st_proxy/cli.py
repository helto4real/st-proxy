from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from collections.abc import Sequence

from .config import BrokerConfig
from .errors import ConfigurationError
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local SillyTavern VRAM handoff broker")
    parser.add_argument("--listen-host", default=_env("LISTEN_HOST", "127.0.0.1"))
    parser.add_argument("--chat-port", type=int, default=_env_int("CHAT_PORT", 5001))
    parser.add_argument("--image-port", type=int, default=_env_int("IMAGE_PORT", 8188))
    parser.add_argument("--kobold-url", default=_env("KOBOLD_URL", "http://127.0.0.1:5002"))
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
    parser.add_argument("--poll-interval", type=float, default=_env_float("POLL_INTERVAL", 0.5))
    parser.add_argument("--log-level", default=_env("LOG_LEVEL", "INFO"))
    return parser


def config_from_args(args: argparse.Namespace) -> BrokerConfig:
    if (_env("TEST_MODE", "")).lower() in {"1", "true", "yes", "on"}:
        raise ConfigurationError("test mode is available only to the in-process test harness")
    return BrokerConfig(
        listen_host=args.listen_host,
        chat_port=args.chat_port,
        image_port=args.image_port,
        kobold_url=args.kobold_url,
        comfy_url=args.comfy_url,
        kobold_admin_password=args.kobold_admin_password,
        request_timeout=args.request_timeout,
        image_timeout=args.image_timeout,
        chat_drain_timeout=args.chat_drain_timeout,
        unload_timeout=args.unload_timeout,
        reload_timeout=args.reload_timeout,
        cleanup_timeout=args.cleanup_timeout,
        poll_interval=args.poll_interval,
    )


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
        asyncio.run(run(config))
    except (ConfigurationError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
