from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from aiohttp import ClientError, ClientSession

from ..errors import UpstreamError
from ..http import child_url
from .base import BackendInfo, BackendTimeouts, RestorePoint, backend_error

LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OllamaRestorePoint:
    model: str
    backend_kind: str = "ollama"


class OllamaBackend:
    def __init__(
        self,
        session: ClientSession,
        *,
        origin: str,
        timeouts: BackendTimeouts,
    ) -> None:
        self._session = session
        self._timeouts = timeouts
        self._info = BackendInfo("ollama", "Ollama", origin)

    @property
    def info(self) -> BackendInfo:
        return self._info

    def _require_target(self, target: RestorePoint) -> OllamaRestorePoint:
        if not isinstance(target, OllamaRestorePoint):
            raise backend_error(self.info.label, "restore target validation", "backend mismatch")
        return target

    async def _get_json(self, path: str, action: str) -> object:
        try:
            async with asyncio.timeout(self._timeouts.request):
                async with self._session.get(
                    child_url(self.info.chat_origin, path)
                ) as response:
                    if response.status >= 400:
                        raise backend_error(
                            self.info.label, action, f"HTTP {response.status}"
                        )
                    return await response.json(content_type=None)
        except UpstreamError:
            raise
        except (ClientError, TimeoutError, ValueError) as exc:
            raise backend_error(self.info.label, action, type(exc).__name__) from exc

    async def _running_models(self) -> tuple[str, ...]:
        payload = await self._get_json("/api/ps", "running-model check")
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            raise backend_error(
                self.info.label, "running-model check", "invalid API response"
            )
        names: list[str] = []
        for item in models:
            value = item.get("model") if isinstance(item, dict) else None
            if value is None and isinstance(item, dict):
                value = item.get("name")
            name = str(value).strip() if value is not None else ""
            if not name:
                raise backend_error(
                    self.info.label, "running-model check", "model identity missing"
                )
            names.append(name)
        return tuple(names)

    async def _set_keep_alive(
        self,
        model: str,
        keep_alive: int,
        action: str,
        timeout_seconds: float,
    ) -> None:
        try:
            async with asyncio.timeout(timeout_seconds):
                async with self._session.post(
                    child_url(self.info.chat_origin, "/api/generate"),
                    json={"model": model, "keep_alive": keep_alive, "stream": False},
                ) as response:
                    await response.read()
                    if response.status >= 400:
                        raise backend_error(
                            self.info.label, action, f"HTTP {response.status}"
                        )
        except UpstreamError:
            raise
        except (ClientError, TimeoutError) as exc:
            raise backend_error(self.info.label, action, type(exc).__name__) from exc

    async def _wait_for_models(
        self,
        expected: tuple[str, ...],
        timeout_seconds: float,
        state_name: str,
    ) -> None:
        started = time.monotonic()
        deadline = started + timeout_seconds
        try:
            async with asyncio.timeout_at(deadline):
                while True:
                    models = await self._running_models()
                    if models == expected:
                        LOG.info(
                            "%s model state confirmed: state=%s duration=%.3fs",
                            self.info.label,
                            state_name,
                            time.monotonic() - started,
                        )
                        return
                    await asyncio.sleep(self._timeouts.poll_interval)
        except TimeoutError as exc:
            raise backend_error(
                self.info.label, f"confirmation of {state_name}", "timed out"
            ) from exc

    async def validate_control(self) -> None:
        payload = await self._get_json("/api/version", "readiness check")
        version = payload.get("version") if isinstance(payload, dict) else None
        if not isinstance(version, str) or not version.strip():
            raise backend_error(self.info.label, "readiness check", "invalid version response")
        await self._running_models()
        LOG.info("%s lifecycle API confirmed", self.info.label)

    async def snapshot_ready(self) -> OllamaRestorePoint:
        models = await self._running_models()
        if not models:
            raise backend_error(
                self.info.label, "ready-state snapshot", "no model is loaded"
            )
        if len(models) != 1:
            raise backend_error(
                self.info.label,
                "ready-state snapshot",
                "exactly one loaded model is required",
            )
        return OllamaRestorePoint(models[0])

    async def release_gpu(self, target: RestorePoint) -> None:
        restore = self._require_target(target)
        LOG.info("%s GPU release requested", self.info.label)
        await self._set_keep_alive(
            restore.model,
            0,
            "model unload",
            self._timeouts.release,
        )
        await self._wait_for_models((), self._timeouts.release, "unloaded")

    async def acquire_gpu(self, target: RestorePoint) -> None:
        restore = self._require_target(target)
        LOG.info("%s GPU acquisition requested", self.info.label)
        await self._set_keep_alive(
            restore.model,
            -1,
            "model load",
            self._timeouts.acquire,
        )
        await self._wait_for_models((restore.model,), self._timeouts.acquire, "loaded")
