from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeVar

from ..errors import UpstreamError


@dataclass(frozen=True, slots=True)
class BackendInfo:
    kind: str
    label: str
    chat_origin: str


@dataclass(frozen=True, slots=True)
class BackendTimeouts:
    request: float
    release: float
    acquire: float
    poll_interval: float


class RestorePoint(Protocol):
    backend_kind: str


RestorePointT = TypeVar("RestorePointT", bound=RestorePoint)


class LlmBackend(Protocol[RestorePointT]):
    @property
    def info(self) -> BackendInfo: ...

    async def validate_control(self) -> None:
        """Confirm that the backend can safely release and reacquire GPU resources."""

    async def snapshot_ready(self) -> RestorePointT:
        """Verify readiness and return the exact state that must later be restored."""

    async def release_gpu(self, target: RestorePointT) -> None:
        """Return only after the backend has confirmed that its GPU resources are released."""

    async def acquire_gpu(self, target: RestorePointT) -> None:
        """Restore target and return only after the backend is confirmed ready."""


def backend_error(label: str, action: str, detail: str | None = None) -> UpstreamError:
    suffix = f": {detail}" if detail else ""
    return UpstreamError(f"{label} {action} failed{suffix}")
