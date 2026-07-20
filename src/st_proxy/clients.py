from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from aiohttp import ClientError, ClientSession

from .config import BrokerConfig
from .errors import UpstreamError
from .http import BufferedResponse, child_url

LOG = logging.getLogger(__name__)


def _safe_error(service: str, action: str, detail: str | None = None) -> UpstreamError:
    suffix = f": {detail}" if detail else ""
    return UpstreamError(f"{service} {action} failed{suffix}")


class KoboldClient:
    def __init__(self, session: ClientSession, config: BrokerConfig) -> None:
        self._session = session
        self._config = config

    @property
    def _admin_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._config.kobold_admin_password:
            headers["Authorization"] = f"Bearer {self._config.kobold_admin_password}"
        return headers

    async def _reload_config(self, filename: str, timeout_seconds: float) -> None:
        LOG.info("KoboldCpp admin operation started: config=%s", filename)
        try:
            async with asyncio.timeout(timeout_seconds):
                async with self._session.post(
                    child_url(self._config.kobold_url, "/api/admin/reload_config"),
                    json={"filename": filename},
                    headers=self._admin_headers,
                ) as response:
                    raw = await response.read()
                    if response.status >= 400:
                        raise _safe_error("KoboldCpp", filename, f"HTTP {response.status}")
                    try:
                        payload = json.loads(raw)
                    except (ValueError, TypeError) as exc:
                        raise _safe_error(
                            "KoboldCpp", filename, "invalid admin response"
                        ) from exc
                    if not isinstance(payload, dict) or payload.get("success") is not True:
                        raise _safe_error("KoboldCpp", filename, "admin request rejected")
                    LOG.info(
                        "KoboldCpp admin operation completed: config=%s status=%s",
                        filename,
                        response.status,
                    )
        except (ClientError, TimeoutError) as exc:
            raise _safe_error("KoboldCpp", filename, type(exc).__name__) from exc

    async def _model_name(self) -> str | None:
        try:
            async with self._session.get(
                child_url(self._config.kobold_url, "/api/v1/model")
            ) as response:
                if response.status >= 400:
                    return None
                payload = await response.json(content_type=None)
        except (ClientError, TimeoutError, ValueError):
            return None
        result = payload.get("result") if isinstance(payload, dict) else None
        if isinstance(result, list):
            result = result[0] if result else None
        return str(result).strip() if result is not None else None

    async def _version_ready(self) -> bool:
        try:
            async with self._session.get(
                child_url(self._config.kobold_url, "/api/v1/info/version")
            ) as response:
                await response.read()
                return response.status < 400
        except (ClientError, TimeoutError):
            return False

    async def ensure_admin_ready(self) -> None:
        setup_guidance = (
            "enable Model Administration, configure its required directory, "
            "and verify the admin password"
        )
        try:
            async with asyncio.timeout(self._config.request_timeout):
                async with self._session.get(
                    child_url(self._config.kobold_url, "/api/extra/version")
                ) as response:
                    if response.status >= 400:
                        raise _safe_error(
                            "KoboldCpp", "admin readiness check", f"HTTP {response.status}"
                        )
                    capabilities = await response.json(content_type=None)
                admin_level = capabilities.get("admin") if isinstance(capabilities, dict) else None
                if not isinstance(admin_level, int) or admin_level < 1:
                    raise _safe_error(
                        "KoboldCpp",
                        "model administration check",
                        f"unavailable; {setup_guidance}",
                    )
                async with self._session.get(
                    child_url(self._config.kobold_url, "/api/admin/list_options"),
                    headers=self._admin_headers,
                ) as response:
                    if response.status >= 400:
                        raise _safe_error(
                            "KoboldCpp", "admin options check", f"HTTP {response.status}"
                        )
                    options = await response.json(content_type=None)
        except UpstreamError:
            raise
        except (ClientError, TimeoutError, ValueError) as exc:
            raise _safe_error(
                "KoboldCpp", "admin readiness check", type(exc).__name__
            ) from exc
        required = {"unload_model", "initial_model"}
        available = set(options) if isinstance(options, list) else set()
        if not required.issubset(available):
            raise _safe_error(
                "KoboldCpp",
                "admin model options check",
                f"required options missing; {setup_guidance}",
            )
        LOG.info("KoboldCpp model administration confirmed: admin_level=%s", admin_level)

    async def _wait_for_model(self, *, loaded: bool, timeout_seconds: float) -> None:
        started = time.monotonic()
        deadline = time.monotonic() + timeout_seconds
        expected_state = "loaded" if loaded else "unloaded"
        LOG.info(
            "waiting for KoboldCpp model state: expected=%s timeout=%.1fs",
            expected_state,
            timeout_seconds,
        )
        inactive_names = {"", "inactive", "unloaded", "none", "no model"}
        while time.monotonic() < deadline:
            model = await self._model_name()
            version_ready = await self._version_ready()
            is_loaded = bool(model) and model.lower() not in inactive_names
            if version_ready and is_loaded is loaded:
                LOG.info(
                    "KoboldCpp model state confirmed: state=%s duration=%.3fs",
                    expected_state,
                    time.monotonic() - started,
                )
                return
            await asyncio.sleep(self._config.poll_interval)
        raise _safe_error("KoboldCpp", f"confirmation of {expected_state}", "timed out")

    async def unload(self) -> None:
        await self._reload_config("unload_model", self._config.unload_timeout)
        await self._wait_for_model(loaded=False, timeout_seconds=self._config.unload_timeout)

    async def ensure_loaded(self) -> None:
        await self._wait_for_model(loaded=True, timeout_seconds=self._config.reload_timeout)

    async def reload_initial(self) -> None:
        await self._reload_config("initial_model", self._config.reload_timeout)
        await self._wait_for_model(loaded=True, timeout_seconds=self._config.reload_timeout)


@dataclass(slots=True)
class PromptResult:
    response: BufferedResponse
    prompt_id: str | None


class ComfyClient:
    def __init__(self, session: ClientSession, config: BrokerConfig) -> None:
        self._session = session
        self._config = config

    async def submit(
        self,
        *,
        path: str,
        query_string: str,
        headers: dict[str, str],
        body: bytes,
    ) -> PromptResult:
        url = child_url(self._config.comfy_url, path)
        if query_string:
            url = f"{url}?{query_string}"
        try:
            async with asyncio.timeout(self._config.request_timeout):
                async with self._session.post(url, headers=headers, data=body) as response:
                    raw = await response.read()
                    buffered = BufferedResponse(
                        response.status,
                        response.reason,
                        tuple(response.headers.items()),
                        raw,
                    )
        except (ClientError, TimeoutError) as exc:
            raise _safe_error("ComfyUI", "prompt submission", type(exc).__name__) from exc
        prompt_id = None
        if buffered.status < 400:
            try:
                payload = json.loads(raw)
                value = payload.get("prompt_id") if isinstance(payload, dict) else None
                prompt_id = str(value) if value is not None else None
            except (ValueError, TypeError):
                pass
        return PromptResult(buffered, prompt_id)

    async def wait_for_prompt(self, prompt_id: str) -> None:
        started = time.monotonic()
        deadline = time.monotonic() + self._config.image_timeout
        while time.monotonic() < deadline:
            try:
                per_request_timeout = min(
                    self._config.request_timeout, max(0.01, deadline - time.monotonic())
                )
                async with asyncio.timeout(per_request_timeout):
                    async with self._session.get(
                        child_url(self._config.comfy_url, f"/history/{prompt_id}")
                    ) as response:
                        if response.status < 400:
                            payload = await response.json(content_type=None)
                            entry = payload.get(prompt_id) if isinstance(payload, dict) else None
                            if isinstance(entry, dict):
                                status = entry.get("status")
                                if isinstance(status, dict):
                                    status_name = str(status.get("status_str", "")).lower()
                                    if status_name in {
                                        "cancelled",
                                        "canceled",
                                        "error",
                                        "failed",
                                        "failure",
                                    }:
                                        raise _safe_error("ComfyUI", "image job", status_name)
                                    if status.get("completed") is True:
                                        if status_name not in {"success", "completed"}:
                                            raise _safe_error(
                                                "ComfyUI",
                                                "image job",
                                                status_name or "unknown",
                                            )
                                        LOG.info(
                                            "ComfyUI job state confirmed: prompt_id=%s "
                                            "duration=%.3fs",
                                            prompt_id,
                                            time.monotonic() - started,
                                        )
                                        return
                                if entry.get("outputs") and not status:
                                    LOG.info(
                                        "ComfyUI job outputs confirmed: prompt_id=%s "
                                        "duration=%.3fs",
                                        prompt_id,
                                        time.monotonic() - started,
                                    )
                                    return
            except UpstreamError:
                raise
            except (ClientError, TimeoutError, ValueError):
                pass
            await asyncio.sleep(self._config.poll_interval)
        await self.interrupt()
        raise _safe_error("ComfyUI", "image job", "timed out")

    async def interrupt(self) -> None:
        LOG.info("ComfyUI interrupt requested")
        try:
            async with asyncio.timeout(self._config.cleanup_timeout):
                async with self._session.post(
                    child_url(self._config.comfy_url, "/interrupt")
                ) as response:
                    await response.read()
                    LOG.info("ComfyUI interrupt completed: status=%s", response.status)
        except (ClientError, TimeoutError) as exc:
            LOG.warning("ComfyUI interrupt failed: %s", type(exc).__name__)

    async def free_models(self) -> None:
        LOG.info("ComfyUI VRAM cleanup requested")
        try:
            async with asyncio.timeout(self._config.cleanup_timeout):
                async with self._session.post(
                    child_url(self._config.comfy_url, "/free"),
                    json={"unload_models": True, "free_memory": True},
                ) as response:
                    await response.read()
                    if response.status >= 400:
                        raise _safe_error("ComfyUI", "model cleanup", f"HTTP {response.status}")
                    LOG.info("ComfyUI VRAM cleanup completed: status=%s", response.status)
        except UpstreamError:
            raise
        except (ClientError, TimeoutError) as exc:
            raise _safe_error("ComfyUI", "model cleanup", type(exc).__name__) from exc
