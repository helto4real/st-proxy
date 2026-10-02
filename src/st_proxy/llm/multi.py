"""One GPU participant backed by the three native model managers.

The coordinator still owns the FIFO and response lifetime. This adapter owns
cross-engine release, while native adapters retain their lifecycle contracts.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .base import BackendInfo, RestorePoint, backend_error
from .koboldcpp import KoboldCppBackend, KoboldCppRestorePoint
from .prism import PrismBackend, PrismRestorePoint
from .tabbyapi import TabbyApiBackend, TabbyApiRestorePoint


@dataclass(frozen=True, slots=True)
class MultiTarget:
    engine: str
    model: str
    backend_kind: str = "all"


class MultiBackend:
    def __init__(self, backends: dict[
        str, KoboldCppBackend | TabbyApiBackend | PrismBackend,
    ]) -> None:
        self.backends = backends
        self.info = BackendInfo("all", "LLM backends", "")
        self.active: MultiTarget | None = None
        self._saved: dict[tuple[str, str], RestorePoint] = {}
        # Retained even when acquisition or forwarding fails. An unpublished
        # load must be reconciled by its adapter before another engine may run.
        self._pending: MultiTarget | None = None
        self._router_failed = False
        self._kobold_released = False

    def request_failed(self, target: MultiTarget) -> None:
        if target.engine == "koboldcpp":
            self._router_failed = True
            self._kobold_released = False

    async def validate_control(self) -> None:
        for backend in self.backends.values():
            await backend.validate_control()

    async def model_catalog(self, engine: str | None = None) -> dict[str, Any]:
        cards = []
        for kind, backend in self.backends.items():
            if engine is not None and kind != engine:
                continue
            if isinstance(backend, KoboldCppBackend):
                payload = await backend.router_models()
            else:
                payload = await backend.available_models()
            for card in payload["data"]:
                cards.append({**card, "id": f"{kind}/{card['id']}", "owned_by": kind})
        return {"object": "list", "data": cards}

    async def request_target(self, model: Any) -> MultiTarget:
        if not isinstance(model, str) or "/" not in model:
            raise ValueError("select a model ID from /v1/models (backend/model)")
        engine, native = model.split("/", 1)
        if engine not in self.backends or not native:
            raise ValueError("unknown backend/model")
        catalog = await self.model_catalog(engine)
        if model not in {card["id"] for card in catalog["data"]}:
            raise ValueError("unknown backend/model")
        return MultiTarget(engine, native)

    async def observe_ready(self) -> MultiTarget | None:
        occupied = self._pending is not None
        for backend in self.backends.values():
            if await backend.observe_ready() is not None:
                occupied = True
        # A group reservation also covers multiple pre-existing loaded engines.
        return (self.active or MultiTarget("", "")) if occupied else None

    async def snapshot_ready(self) -> MultiTarget:
        observed = await self.observe_ready()
        if observed is None:
            raise backend_error(self.info.label, "readiness", "no active model")
        return observed

    async def _release(self, engine: str) -> None:
        backend = self.backends[engine]
        if isinstance(backend, KoboldCppBackend):
            # Native unload restarts the child even when already empty. Confirm
            # once, then retain that result until another router reservation.
            # Repeating it while other engines run can race the child's restart.
            if not self._kobold_released:
                await backend.release_gpu(KoboldCppRestorePoint(""))
                self._kobold_released = True
            self._router_failed = False
        else:
            observed = await backend.observe_ready()
            if observed is not None:
                if self.active and self.active.engine == engine:
                    self._saved[(engine, self.active.model)] = observed
                await backend.release_gpu(observed)
        if self.active and self.active.engine == engine:
            self.active = None
        if self._pending and self._pending.engine == engine:
            self._pending = None

    async def release_gpu(self, target: RestorePoint) -> None:
        if not isinstance(target, MultiTarget):
            raise backend_error(self.info.label, "release", "backend mismatch")
        # Check every engine: startup can attach to previously running services.
        for engine in self.backends:
            await self._release(engine)

    async def acquire_gpu(self, target: RestorePoint | None) -> MultiTarget:
        if not isinstance(target, MultiTarget) or target.engine not in self.backends:
            raise backend_error(self.info.label, "model selection", "explicit model required")
        for engine in self.backends:
            if engine != target.engine:
                await self._release(engine)
        backend = self.backends[target.engine]
        if isinstance(backend, KoboldCppBackend) and (
            self._router_failed or self.active == target
        ):
            # Any native request failure requires confirmed unload, even when
            # the next request selects another profile on the same engine.
            await self._release(target.engine)
        if self.active and self.active.engine == target.engine and self.active != target:
            observed = await backend.observe_ready()
            if observed is not None:
                self._saved[(target.engine, self.active.model)] = observed
        native = self._saved.get((target.engine, target.model))
        if native is None:
            if isinstance(backend, TabbyApiBackend):
                native = TabbyApiRestorePoint(target.model, {})
            elif isinstance(backend, PrismBackend):
                native = PrismRestorePoint(target.model)
            else:
                native = KoboldCppRestorePoint(target.model)
        self._pending = target
        self.active = None
        if isinstance(backend, KoboldCppBackend):
            self._kobold_released = False
        restored = await backend.acquire_gpu(native)
        self._saved[(target.engine, target.model)] = restored
        self.active = target
        return target
