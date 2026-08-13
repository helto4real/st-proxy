from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from aiohttp import ClientError, ClientSession

from ..errors import UpstreamError
from ..http import child_url
from .base import BackendInfo, BackendTimeouts, RestorePoint, backend_error

LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class KoboldCppRestorePoint:
    model: str
    backend_kind: str = "koboldcpp"


class KoboldCppBackend:
    _INACTIVE_MODEL_NAMES = {"", "inactive", "unloaded", "none", "no model"}

    def __init__(
        self,
        session: ClientSession,
        *,
        origin: str,
        admin_password: str | None,
        timeouts: BackendTimeouts,
    ) -> None:
        self._session = session
        self._admin_password = admin_password
        self._timeouts = timeouts
        self._info = BackendInfo("koboldcpp", "KoboldCpp", origin)

    @property
    def info(self) -> BackendInfo:
        return self._info

    @property
    def _admin_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._admin_password:
            headers["Authorization"] = f"Bearer {self._admin_password}"
        return headers

    def _require_target(self, target: RestorePoint) -> KoboldCppRestorePoint:
        if not isinstance(target, KoboldCppRestorePoint):
            raise backend_error(self.info.label, "restore target validation", "backend mismatch")
        return target

    async def _reload_config(self, filename: str, timeout_seconds: float) -> None:
        LOG.info("%s admin operation started: config=%s", self.info.label, filename)
        try:
            async with asyncio.timeout(timeout_seconds):
                async with self._session.post(
                    child_url(self.info.chat_origin, "/api/admin/reload_config"),
                    json={"filename": filename},
                    headers=self._admin_headers,
                ) as response:
                    raw = await response.read()
                    if response.status >= 400:
                        raise backend_error(self.info.label, filename, f"HTTP {response.status}")
                    try:
                        payload = json.loads(raw)
                    except (ValueError, TypeError) as exc:
                        raise backend_error(
                            self.info.label, filename, "invalid admin response"
                        ) from exc
                    if not isinstance(payload, dict) or payload.get("success") is not True:
                        raise backend_error(
                            self.info.label, filename, "admin request rejected"
                        )
                    LOG.info(
                        "%s admin operation completed: config=%s status=%s",
                        self.info.label,
                        filename,
                        response.status,
                    )
        except (ClientError, TimeoutError) as exc:
            raise backend_error(self.info.label, filename, type(exc).__name__) from exc

    async def _read_model_name(self) -> str:
        try:
            async with self._session.get(
                child_url(self.info.chat_origin, "/api/v1/model")
            ) as response:
                if response.status >= 400:
                    raise backend_error(
                        self.info.label,
                        "model-state observation",
                        f"HTTP {response.status}",
                    )
                payload = await response.json(content_type=None)
        except UpstreamError:
            raise
        except (ClientError, TimeoutError, ValueError) as exc:
            raise backend_error(
                self.info.label,
                "model-state observation",
                type(exc).__name__,
            ) from exc
        result = payload.get("result") if isinstance(payload, dict) else None
        if isinstance(result, list):
            result = result[0] if result else None
        if result is None:
            raise backend_error(
                self.info.label,
                "model-state observation",
                "invalid API response",
            )
        return str(result).strip()

    async def _model_name(self) -> str | None:
        try:
            return await self._read_model_name()
        except UpstreamError:
            return None

    async def _version_ready(self) -> bool:
        try:
            async with self._session.get(
                child_url(self.info.chat_origin, "/api/v1/info/version")
            ) as response:
                await response.read()
                return response.status < 400
        except (ClientError, TimeoutError):
            return False

    async def observe_ready(self) -> KoboldCppRestorePoint | None:
        """Inspect the current state once; never wait for or trigger a model load."""
        model = await self._read_model_name()
        if not await self._version_ready():
            raise backend_error(self.info.label, "ready-state observation", "unavailable")
        if not model or model.lower() in self._INACTIVE_MODEL_NAMES:
            return None
        return KoboldCppRestorePoint(model)

    async def validate_control(self) -> None:
        setup_guidance = (
            "enable Model Administration, configure its required directory, "
            "and verify the admin password"
        )
        try:
            async with asyncio.timeout(self._timeouts.request):
                async with self._session.get(
                    child_url(self.info.chat_origin, "/api/extra/version")
                ) as response:
                    if response.status >= 400:
                        raise backend_error(
                            self.info.label,
                            "admin readiness check",
                            f"HTTP {response.status}",
                        )
                    capabilities = await response.json(content_type=None)
                admin_level = capabilities.get("admin") if isinstance(capabilities, dict) else None
                if not isinstance(admin_level, int) or admin_level < 1:
                    raise backend_error(
                        self.info.label,
                        "model administration check",
                        f"unavailable; {setup_guidance}",
                    )
                async with self._session.get(
                    child_url(self.info.chat_origin, "/api/admin/list_options"),
                    headers=self._admin_headers,
                ) as response:
                    if response.status >= 400:
                        raise backend_error(
                            self.info.label,
                            "admin options check",
                            f"HTTP {response.status}",
                        )
                    options = await response.json(content_type=None)
        except UpstreamError:
            raise
        except (ClientError, TimeoutError, ValueError) as exc:
            raise backend_error(
                self.info.label, "admin readiness check", type(exc).__name__
            ) from exc
        required = {"unload_model", "initial_model"}
        available = set(options) if isinstance(options, list) else set()
        if not required.issubset(available):
            raise backend_error(
                self.info.label,
                "admin model options check",
                f"required options missing; {setup_guidance}",
            )
        LOG.info("%s model administration confirmed: admin_level=%s", self.info.label, admin_level)

    async def _wait_for_model(
        self,
        *,
        loaded: bool,
        timeout_seconds: float,
        expected_model: str | None = None,
    ) -> str | None:
        started = time.monotonic()
        deadline = started + timeout_seconds
        expected_state = "loaded" if loaded else "unloaded"
        LOG.info(
            "waiting for %s model state: expected=%s timeout=%.1fs",
            self.info.label,
            expected_state,
            timeout_seconds,
        )
        try:
            async with asyncio.timeout_at(deadline):
                while True:
                    model = await self._model_name()
                    version_ready = await self._version_ready()
                    is_loaded = bool(model) and model.lower() not in self._INACTIVE_MODEL_NAMES
                    expected_model_ready = (
                        not loaded or expected_model is None or model == expected_model
                    )
                    if version_ready and is_loaded is loaded and expected_model_ready:
                        LOG.info(
                            "%s model state confirmed: state=%s duration=%.3fs",
                            self.info.label,
                            expected_state,
                            time.monotonic() - started,
                        )
                        return model
                    await asyncio.sleep(self._timeouts.poll_interval)
        except TimeoutError as exc:
            raise backend_error(
                self.info.label, f"confirmation of {expected_state}", "timed out"
            ) from exc

    async def snapshot_ready(self) -> KoboldCppRestorePoint:
        model = await self._wait_for_model(
            loaded=True,
            timeout_seconds=self._timeouts.acquire,
        )
        if not model:
            raise backend_error(self.info.label, "ready-state snapshot", "model is unknown")
        return KoboldCppRestorePoint(model)

    async def release_gpu(self, target: RestorePoint) -> None:
        self._require_target(target)
        await self._reload_config("unload_model", self._timeouts.release)
        await self._wait_for_model(loaded=False, timeout_seconds=self._timeouts.release)

    async def acquire_gpu(
        self,
        target: RestorePoint | None,
    ) -> KoboldCppRestorePoint:
        restore = self._require_target(target) if target is not None else None
        await self._reload_config("initial_model", self._timeouts.acquire)
        model = await self._wait_for_model(
            loaded=True,
            timeout_seconds=self._timeouts.acquire,
            expected_model=restore.model if restore is not None else None,
        )
        if not model:
            raise backend_error(self.info.label, "model load", "model is unknown")
        return restore or KoboldCppRestorePoint(model)
