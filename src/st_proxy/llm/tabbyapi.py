from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientSession

from ..errors import InsufficientVram
from ..http import child_url
from .base import BackendInfo, BackendTimeouts, RestorePoint, backend_error

_BUDGET_KEYS = (
    "reasoning_budget_tokens", "reasoning_budget", "thinking_budget", "thinking_token_budget",
)
_OUTPUT_KEYS = ("max_tokens", "max_completion_tokens", "max_length")
_EFFORT_DIVISORS = {"minimal": 10, "low": 4, "medium": 2}
LOG = logging.getLogger(__name__)


def _first_present(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    # Tabby selects the first present alias, including explicit null.
    return next((payload[key] for key in keys if key in payload), None)


def _integer(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
    return None


def _budget_falls_back(value: Any) -> bool:
    integer = _integer(value)
    return value is None or (integer is not None and integer < 0)


def adapt_chat_request(body: bytes) -> bytes:
    """Translate thinking controls for Tabby chat without changing output limits.

    Preserve native alias/fallback semantics and leave backend validation to
    Tabby. No change means the original bytes (including formatting) survive.
    """
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        return body
    if not isinstance(payload, dict):
        return body
    reasoning = payload.get("reasoning")
    if reasoning is None:
        reasoning = {}
    template = _first_present(payload, ("template_vars", "chat_template_kwargs"))
    if not isinstance(reasoning, dict) or (template is not None and not isinstance(template, dict)):
        return body
    template = template or {}
    effort = reasoning.get("effort")
    enabled = reasoning.get("enabled")
    if payload.get("reasoning_effort") is not None:
        effort = payload["reasoning_effort"]
    if payload.get("enable_thinking") is not None:
        enabled = payload["enable_thinking"]
    effort = template.get("reasoning_effort", effort)
    enabled = template.get("enable_thinking", enabled)
    effort = effort.strip().lower() if isinstance(effort, str) else None
    changed = False
    if effort in {"none", "off"} and enabled is None and "enable_thinking" not in template:
        payload["enable_thinking"] = False
        changed = True

    native = _first_present(payload, _BUDGET_KEYS)
    nested = reasoning.get("max_tokens")
    # Invalid native values also stay with Tabby's validator; never mask them.
    native_wins = not _budget_falls_back(native) or not _budget_falls_back(nested)
    if not native_wins and "thinking_budget_tokens" in payload:
        payload["reasoning_budget_tokens"] = payload.pop("thinking_budget_tokens")
        changed = True
    elif (
        not native_wins
        and not any(key in payload for key in _BUDGET_KEYS)
        and "max_tokens" not in reasoning
        and enabled is not False
        and effort in _EFFORT_DIVISORS
    ):
        output_limit = _integer(_first_present(payload, _OUTPUT_KEYS))
        if output_limit is not None and output_limit > 0:
            payload["reasoning_budget_tokens"] = output_limit // _EFFORT_DIVISORS[effort]
            changed = True
    if not changed:
        return body
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()


@dataclass(slots=True)
class TabbyApiRestorePoint:
    model: str
    parameters: dict[str, Any]
    backend_kind: str = "tabbyapi"


class TabbyApiBackend:
    """TabbyAPI's explicit load/unload lifecycle.

    Offload and other settings absent from the model API remain TabbyAPI's
    responsibility (model.use_as_default / model-local configuration).
    """

    def __init__(
        self,
        session: ClientSession,
        *,
        origin: str,
        model: str | None,
        max_seq_len: int,
        timeouts: BackendTimeouts,
    ) -> None:
        self._session = session
        self._info = BackendInfo("tabbyapi", "TabbyAPI", origin)
        self._model = model
        self._max_seq_len = max_seq_len
        self._timeouts = timeouts
        # A lost load connection does not cancel TabbyAPI's detached load task.
        # Do not let a later 503 observation turn that into a successful release.
        self._uncertain = False
        self._pending_load: TabbyApiRestorePoint | None = None
        self._load_failed = False

    @property
    def info(self) -> BackendInfo:
        return self._info

    async def _observe_confirmed(
        self, *, retry_failed_load: bool = False,
    ) -> TabbyApiRestorePoint | None:
        current = await self._observe()
        if not self._uncertain:
            return current
        pending = self._pending_load
        if current is None:
            # A failed load may retain allocations. Only LLM acquisition can retry;
            # do not grant ComfyUI a lease merely because no model is published.
            confirmed = pending is None or (retry_failed_load and self._load_failed)
        else:
            confirmed = pending is not None and current.model == pending.model and all(
                current.parameters.get(key) == value
                for key, value in pending.parameters.items()
            )
        if not confirmed:
            raise backend_error(
                self.info.label, "lifecycle confirmation",
                "load/unload is still unconfirmed; status will be checked on the next request",
            )
        self._uncertain = False
        self._pending_load = None
        self._load_failed = False
        return current

    async def _json(self, path: str, *, allow_unloaded: bool = False) -> Any:
        async with asyncio.timeout(self._timeouts.request):
            async with self._session.get(child_url(self.info.chat_origin, path)) as response:
                payload = await response.json(content_type=None)
                if (
                    allow_unloaded and response.status == 503
                    and isinstance(payload, dict)
                    and payload.get("detail") == "No models are currently loaded."
                ):
                    return None
                if response.status != 200:
                    raise backend_error(self.info.label, path, f"HTTP {response.status}")
                return payload

    async def validate_control(self) -> None:
        """Check the passive API, without probing load/unload by mutation."""
        try:
            health = await self._json("/health")
            models = await self._json("/v1/models")
            if not isinstance(health, dict) or health.get("status") != "healthy":
                raise ValueError("invalid health response")
            if not isinstance(models, dict) or not isinstance(models.get("data"), list):
                raise ValueError("invalid model list")
        except (ClientError, TimeoutError, ValueError) as exc:
            raise backend_error(self.info.label, "API check", type(exc).__name__) from exc

    async def _observe(self) -> TabbyApiRestorePoint | None:
        payload = await self._json("/v1/model", allow_unloaded=True)
        if payload is None:
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
            raise ValueError("invalid model card")
        params = payload.get("parameters")
        if not payload["id"] or not isinstance(params, dict):
            raise ValueError("missing model parameters")
        context = params.get("max_seq_len")
        if type(context) is not int or context <= 0:
            raise ValueError("invalid context length")
        # These fields are actually reported by ExLlamaV3 and accepted by load.
        # Do not replay model-card defaults for unreported fields (e.g. rope).
        saved = {
            key: params[key]
            for key in ("max_seq_len", "cache_size", "cache_mode", "chunk_size")
            if params.get(key) is not None
        }
        if params.get("use_vision") is not None:
            saved["vision"] = params["use_vision"]
        return TabbyApiRestorePoint(payload["id"], saved)

    async def observe_ready(self) -> TabbyApiRestorePoint | None:
        try:
            return await self._observe_confirmed()
        except (ClientError, TimeoutError, ValueError) as exc:
            raise backend_error(self.info.label, "model observation", type(exc).__name__) from exc

    async def snapshot_ready(self) -> TabbyApiRestorePoint:
        try:
            async with asyncio.timeout(self._timeouts.acquire):
                while True:
                    observed = await self._observe_confirmed()
                    if observed is not None:
                        return observed
                    await asyncio.sleep(self._timeouts.poll_interval)
        except (ClientError, TimeoutError, ValueError) as exc:
            raise backend_error(self.info.label, "readiness", type(exc).__name__) from exc

    def _target(self, target: RestorePoint) -> TabbyApiRestorePoint:
        if not isinstance(target, TabbyApiRestorePoint):
            raise backend_error(self.info.label, "restore target validation", "backend mismatch")
        return target

    async def release_gpu(self, target: RestorePoint) -> None:
        restore = self._target(target)
        try:
            async with asyncio.timeout(self._timeouts.release):
                current = await self._observe_confirmed()
                if current is None:
                    return
                # Native model selection may have changed since the last lease.
                # Keep the coordinator's restore point in sync before unloading.
                restore.model = current.model
                restore.parameters = current.parameters
                self._uncertain = True
                self._pending_load = None
                async with self._session.post(
                    child_url(self.info.chat_origin, "/v1/model/unload"),
                ) as response:
                    await response.read()
                    if response.status != 200:
                        raise backend_error(
                            self.info.label, "unload", f"HTTP {response.status}",
                        )
                if await self._observe() is not None:
                    raise backend_error(self.info.label, "unload", "model is still loaded")
                self._uncertain = False
        except (ClientError, TimeoutError, ValueError) as exc:
            raise backend_error(self.info.label, "unload", type(exc).__name__) from exc

    async def _load(self, restore: TabbyApiRestorePoint) -> None:
        payload = {
            **restore.parameters,
            "model_name": restore.model,
            "backend": "exllamav3",
            "skip_queue": False,
        }
        async with self._session.post(
            child_url(self.info.chat_origin, "/v1/model/load"), json=payload,
        ) as response:
            if response.status != 200:
                await response.read()
                raise backend_error(self.info.label, "load", f"HTTP {response.status}")
            if response.content_type != "text/event-stream":
                raise ValueError("expected load SSE")
            data: list[str] = []
            finished = False
            failed = False
            insufficient_vram = False
            async for raw_line in response.content:
                line = raw_line.decode("utf-8").rstrip("\r\n")
                if line.startswith("event:") and line[6:].strip() == "error":
                    raise backend_error(self.info.label, "load", "SSE error")
                if line.startswith("data:"):
                    data.append(line[5:].lstrip(" "))
                elif not line and data:
                    event = json.loads("\n".join(data))
                    data.clear()
                    if not isinstance(event, dict):
                        raise backend_error(self.info.label, "load", "invalid SSE event")
                    if "error" in event:
                        error = event["error"]
                        if not isinstance(error, dict) or not isinstance(error.get("message"), str):
                            raise backend_error(self.info.label, "load", "invalid SSE error")
                        failed = True
                        message = error["message"].lower()
                        insufficient_vram = insufficient_vram or any(
                            marker in message for marker in (
                                "cuda out of memory", "cuda error: out of memory",
                                "insufficient vram", "torch.cuda.outofmemoryerror",
                            )
                        )
                        # Never print the raw backend message or traceback: either
                        # may contain private paths, model names or request data.
                        LOG.error(
                            "%s load error: %s; waiting for load stream to end",
                            self.info.label,
                            "insufficient_vram (not enough GPU memory for model/cache)"
                            if insufficient_vram else "backend reported a load failure",
                        )
                    elif failed:
                        raise backend_error(self.info.label, "load", "progress after SSE error")
                    elif event.get("model_type") == "model" and event.get("status") == "finished":
                        finished = True
            if data or not (finished or failed):
                raise backend_error(self.info.label, "load", "incomplete SSE response")
            if failed:
                # Only a well-formed terminal error AND a clean stream end prove
                # the detached load stopped. Disconnect/timeout stays uncertain.
                self._load_failed = True
                if insufficient_vram:
                    raise InsufficientVram(
                        "TabbyAPI load failed: insufficient_vram "
                        "(not enough GPU memory for model/cache); model is not ready; "
                        "next active LLM request will retry"
                    )
                raise backend_error(
                    self.info.label, "load",
                    "backend reported a completed load failure; next LLM request may retry",
                )

    async def acquire_gpu(self, target: RestorePoint | None) -> TabbyApiRestorePoint:
        restore = self._target(target) if target is not None else None
        # A first load can apply Tabby's model-local overrides to the fallback
        # context. Subsequent restores must match every saved setting exactly.
        expected_parameters = restore.parameters if restore is not None else {}
        try:
            async with asyncio.timeout(self._timeouts.acquire):
                current = await self._observe_confirmed(retry_failed_load=True)
                if restore is None:
                    # Cold recovery must retain a model selected natively in Tabby,
                    # just like the coordinator's initial passive discovery.
                    if current is not None:
                        return current
                    if not self._model:
                        raise backend_error(
                            self.info.label, "load", "no loaded model; configure --tabby-model",
                        )
                    restore = TabbyApiRestorePoint(
                        self._model, {"max_seq_len": self._max_seq_len},
                    )
                if current is not None:
                    if current.model == restore.model and all(
                        current.parameters.get(key) == value
                        for key, value in expected_parameters.items()
                    ):
                        return current
                    # TabbyAPI ignores changed load settings for an already loaded model.
                    await self.release_gpu(current)
                self._uncertain = True
                self._pending_load = TabbyApiRestorePoint(
                    restore.model, dict(expected_parameters),
                )
                self._load_failed = False
                await self._load(restore)
                while True:
                    current = await self._observe()
                    if current is not None:
                        if current.model != restore.model or any(
                            current.parameters.get(key) != value
                            for key, value in expected_parameters.items()
                        ):
                            raise backend_error(
                                self.info.label, "load verification", "model/settings mismatch",
                            )
                        self._uncertain = False
                        self._pending_load = None
                        return current
                    await asyncio.sleep(self._timeouts.poll_interval)
        except (ClientError, TimeoutError, ValueError) as exc:
            raise backend_error(self.info.label, "load", type(exc).__name__) from exc
