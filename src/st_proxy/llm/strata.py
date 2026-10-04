from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientSession

from ..http import child_url
from .base import BackendInfo, BackendTimeouts, RestorePoint, backend_error

STRATA_KINDS = ("sc117", "orca")
STRATA_TEXT_PATHS = frozenset({
    "/v1/chat/completions", "/v1/messages", "/v1/messages/count_tokens",
})


@dataclass(frozen=True, slots=True)
class StrataRestorePoint:
    model: str
    backend_kind: str


class StrataBackend:
    """One optimized Strata installation, started with --lazy.

    Load/unload are synchronous native operations. A failed or cancelled operation
    leaves ownership uncertain: passive health alone cannot prove a late load ended.
    """

    def __init__(
        self, session: ClientSession, *, kind: str, label: str, origin: str,
        timeouts: BackendTimeouts,
    ) -> None:
        self._session = session
        self.info = BackendInfo(kind, label, origin)
        self._timeouts = timeouts
        self._uncertain = False

    def _require_certain(self) -> None:
        if self._uncertain:
            raise backend_error(self.info.label, "lifecycle confirmation",
                                "previous load/unload did not complete; resolve backend state")

    async def _json(self, path: str, *, model: str | None = None) -> Any:
        try:
            async with asyncio.timeout(self._timeouts.request):
                if model is None:
                    call = self._session.get(child_url(self.info.chat_origin, path))
                else:
                    call = self._session.post(child_url(self.info.chat_origin, path),
                                              json={"model": model})
                async with call as response:
                    if response.status != 200:
                        raise backend_error(self.info.label, path, f"HTTP {response.status}")
                    return await response.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as exc:
            raise backend_error(self.info.label, path, type(exc).__name__) from exc

    async def _health(self) -> dict[str, Any]:
        payload = await self._json("/health")
        if (not isinstance(payload, dict) or payload.get("service") != "strata"
                or payload.get("status") != "ok" or type(payload.get("loaded")) is not bool
                or not isinstance(payload.get("model"), str) or not payload["model"]):
            raise backend_error(self.info.label, "model observation", "invalid Strata health")
        return payload

    async def available_models(self) -> dict[str, Any]:
        payload = await self._json("/v1/models")
        cards = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(cards, list) or not cards or any(
            not isinstance(card, dict) or not isinstance(card.get("id"), str) or not card["id"]
            for card in cards
        ):
            raise backend_error(self.info.label, "model catalog", "expected native model IDs")
        return payload

    async def request_target(self, model: Any) -> StrataRestorePoint:
        health = await self._health()
        if model is None:
            model = health["model"]
        if not isinstance(model, str) or model not in {
            card["id"] for card in (await self.available_models())["data"]
        }:
            raise ValueError("unknown Strata model")
        # The native lifecycle API accepts the canonical name, not aliases.
        return StrataRestorePoint(health["model"], self.info.kind)

    async def validate_control(self) -> None:
        self._require_certain()
        await self._health()
        await self.available_models()

    async def observe_ready(self) -> StrataRestorePoint | None:
        self._require_certain()
        health = await self._health()
        return StrataRestorePoint(health["model"], self.info.kind) if health["loaded"] else None

    async def snapshot_ready(self) -> StrataRestorePoint:
        observed = await self.observe_ready()
        if observed is None:
            raise backend_error(self.info.label, "readiness", "no active model")
        return observed

    async def _target(self, target: RestorePoint | None) -> StrataRestorePoint:
        if target is None:
            return await self.request_target(None)
        if not isinstance(target, StrataRestorePoint) or target.backend_kind != self.info.kind:
            raise backend_error(self.info.label, "restore target", "backend mismatch")
        if target.model != (await self._health())["model"]:
            raise backend_error(self.info.label, "restore target", "model mismatch")
        return target

    async def _change(self, target: StrataRestorePoint, *, loaded: bool) -> None:
        self._require_certain()
        self._uncertain = True
        action = "load" if loaded else "unload"
        timeout = self._timeouts.acquire if loaded else self._timeouts.release
        try:
            async with asyncio.timeout(timeout):
                payload = await self._json(f"/v1/{action}", model=target.model)
                accepted = {"loaded"} if loaded else {"unloaded", "not loaded"}
                if not isinstance(payload, dict) or payload.get("status") not in accepted:
                    raise backend_error(self.info.label, action, "operation not confirmed")
                health = await self._health()
                if health["loaded"] != loaded or health["model"] != target.model:
                    raise backend_error(self.info.label, action, "state not confirmed")
        except TimeoutError as exc:
            raise backend_error(self.info.label, action, "timed out") from exc
        else:
            self._uncertain = False

    async def release_gpu(self, target: RestorePoint) -> None:
        self._require_certain()
        restore = await self._target(target)
        await self._change(restore, loaded=False)

    async def acquire_gpu(self, target: RestorePoint | None) -> StrataRestorePoint:
        self._require_certain()
        restore = await self._target(target)
        if await self.observe_ready() is None:
            await self._change(restore, loaded=True)
        return restore
