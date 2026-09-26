from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientSession

from ..http import child_url
from .base import BackendInfo, BackendTimeouts, RestorePoint, backend_error


@dataclass(frozen=True, slots=True)
class PrismRestorePoint:
    model: str
    backend_kind: str = "prism"


class PrismBackend:
    """Prism router presets with one loaded worker, owned by the broker's lease.

    Run with --models-max 1, --no-models-autoload and load-on-startup=false.
    Native unload terminates the model worker; the router stays available.
    """

    def __init__(
        self, session: ClientSession, *, origin: str, model: str, timeouts: BackendTimeouts,
    ) -> None:
        self._session = session
        self._info = BackendInfo("prism", "Prism", origin)
        self._model = model
        self._timeouts = timeouts
        self._uncertain = False

    @property
    def info(self) -> BackendInfo:
        return self._info

    def _require_certain(self) -> None:
        if self._uncertain:
            raise backend_error(
                self.info.label, "lifecycle confirmation",
                "previous load/unload did not complete; resolve backend state before restart",
            )

    def _target(self, target: RestorePoint | None) -> PrismRestorePoint:
        if target is None:
            return PrismRestorePoint(self._model)
        if not isinstance(target, PrismRestorePoint) or not target.model:
            raise backend_error(self.info.label, "restore target", "backend/model mismatch")
        return target

    async def request_target(self, model: Any) -> PrismRestorePoint:
        """Resolve an API model ID without loading or changing GPU state."""
        if model is None:
            model = self._model
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a nonempty Prism preset ID")
        # Observation is safe while a different queued request is loading a worker.
        if model not in await self._states():
            raise ValueError("unknown Prism model")
        return PrismRestorePoint(model)

    async def _json(self, path: str, *, model: str | None = None) -> Any:
        try:
            async with asyncio.timeout(self._timeouts.request):
                if model is None:
                    call = self._session.get(child_url(self.info.chat_origin, path))
                else:
                    call = self._session.post(
                        child_url(self.info.chat_origin, path), json={"model": model},
                    )
                async with call as response:
                    if response.status != 200:
                        raise backend_error(self.info.label, path, f"HTTP {response.status}")
                    return await response.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as exc:
            raise backend_error(self.info.label, path, type(exc).__name__) from exc

    async def _states(self) -> dict[str, dict[str, Any]]:
        payload = await self._json("/models")
        models = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(models, list) or not models:
            raise backend_error(self.info.label, "model observation", "expected router presets")
        states: dict[str, dict[str, Any]] = {}
        for card in models:
            if not isinstance(card, dict):
                raise backend_error(self.info.label, "model observation", "invalid router preset")
            model, state = card.get("id"), card.get("status")
            if not isinstance(model, str) or not model or model in states:
                raise backend_error(self.info.label, "model observation", "invalid preset ID")
            if not isinstance(state, dict) or state.get("value") not in {
                "loaded", "loading", "unloaded", "sleeping",
            }:
                raise backend_error(self.info.label, "model observation", "invalid router state")
            states[model] = state
        if self._model not in states:
            raise backend_error(self.info.label, "model observation", "default preset missing")
        if sum(state["value"] != "unloaded" for state in states.values()) > 1:
            raise backend_error(self.info.label, "model observation", "multiple active workers")
        return states

    @staticmethod
    def _active(states: dict[str, dict[str, Any]]) -> str | None:
        return next((model for model, state in states.items() if state["value"] != "unloaded"),
                    None)

    async def validate_control(self) -> None:
        self._require_certain()
        await self._states()

    async def observe_ready(self) -> PrismRestorePoint | None:
        self._require_certain()
        states = await self._states()
        active = self._active(states)
        if active is None:
            return None
        if states[active]["value"] == "loaded":
            return PrismRestorePoint(active)
        raise backend_error(self.info.label, "model observation", "model not in a stable state")

    async def snapshot_ready(self) -> PrismRestorePoint:
        self._require_certain()
        states = await self._states()
        model = self._active(states) or self._model
        await self._wait(model, "loaded", self._timeouts.acquire)
        return PrismRestorePoint(model)

    async def _wait(self, model: str, expected: str, timeout_seconds: float) -> None:
        try:
            async with asyncio.timeout(timeout_seconds):
                while True:
                    states = await self._states()
                    if model not in states:
                        raise backend_error(self.info.label, "confirmation", "preset disappeared")
                    state = states[model]
                    if state["value"] == expected:
                        if expected == "unloaded" and self._active(states) is not None:
                            raise backend_error(
                                self.info.label, "unload", "another worker is active",
                            )
                        return
                    if expected == "loaded" and state.get("failed") is True:
                        raise backend_error(self.info.label, "model load", "worker failed")
                    await asyncio.sleep(self._timeouts.poll_interval)
        except TimeoutError as exc:
            raise backend_error(
                self.info.label, f"confirmation of {expected}", "timed out",
            ) from exc

    async def _change(
        self, model: str, action: str, expected: str, timeout_seconds: float,
    ) -> None:
        self._require_certain()
        self._uncertain = True
        try:
            async with asyncio.timeout(timeout_seconds):
                payload = await self._json(f"/models/{action}", model=model)
                if not isinstance(payload, dict) or payload.get("success") is not True:
                    raise backend_error(self.info.label, action, "operation not accepted")
                await self._wait(model, expected, timeout_seconds)
        except TimeoutError as exc:
            raise backend_error(self.info.label, action, "timed out") from exc
        else:
            self._uncertain = False

    async def release_gpu(self, target: RestorePoint) -> None:
        restore = self._target(target)
        self._require_certain()
        states = await self._states()
        if restore.model not in states:
            raise backend_error(self.info.label, "restore target", "preset missing")
        active = self._active(states)
        if active is None:
            return
        if active != restore.model:
            raise backend_error(
                self.info.label, "unload", "active model differs from restore point",
            )
        await self._change(active, "unload", "unloaded", self._timeouts.release)

    async def acquire_gpu(self, target: RestorePoint | None) -> PrismRestorePoint:
        restore = self._target(target)
        self._require_certain()
        states = await self._states()
        if restore.model not in states:
            raise backend_error(self.info.label, "restore target", "preset missing")
        active = self._active(states)
        if active is not None:
            if states[active]["value"] != "loaded":
                raise backend_error(
                    self.info.label, "model observation", "model not in a stable state",
                )
            if active == restore.model:
                return restore
            await self._change(active, "unload", "unloaded", self._timeouts.release)
        await self._change(restore.model, "load", "loaded", self._timeouts.acquire)
        return restore
