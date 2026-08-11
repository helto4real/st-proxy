from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .comfy import ComfyClient
from .config import BrokerConfig
from .errors import ChatUnavailable, HandoffError, QueueFull
from .http import BufferedResponse
from .llm import LlmBackend, RestorePoint

LOG = logging.getLogger(__name__)


class HandoffState(StrEnum):
    INITIALIZING = "initializing"
    VERIFYING_LLM = "verifying_llm"
    LLM_READY = "llm_ready"
    DRAINING_LLM = "draining_llm"
    UNLOADING_LLM = "unloading_llm"
    COMFY_READY = "comfy_ready"
    IMAGE_ACTIVE = "image_active"
    CLEANING_COMFY = "cleaning_comfy"
    RELOADING_LLM = "reloading_llm"
    ERROR = "error"
    SHUTTING_DOWN = "shutting_down"


class GpuOwner(StrEnum):
    LLM = "llm"
    COMFY = "comfy"
    UNKNOWN = "unknown"


class _DispatchSignal(StrEnum):
    IDLE_RESTORE = "idle_restore"


@dataclass(slots=True)
class SubmissionOutcome:
    response: BufferedResponse | None = None
    error: str | None = None


@dataclass(slots=True)
class ChatWork:
    sequence: int
    ready: asyncio.Future[str | None]
    cancelled: bool = False
    granted: bool = False


@dataclass(slots=True)
class ImageWork:
    sequence: int
    path: str
    query_string: str
    headers: dict[str, str]
    body: bytes
    submitted: asyncio.Future[SubmissionOutcome]
    cancelled: bool = False


QueuedWork = ChatWork | ImageWork
DispatchItem = QueuedWork | _DispatchSignal


class HandoffCoordinator:
    def __init__(
        self,
        config: BrokerConfig,
        llm: LlmBackend[Any],
        comfy: ComfyClient,
    ) -> None:
        self._config = config
        self._llm = llm
        self._comfy = comfy
        self._restore_point: RestorePoint | None = None
        self._condition = asyncio.Condition()
        self._queue: deque[QueuedWork] = deque()
        self._sequence = 0
        self._active_chats = 0
        self._waiting_chats = 0
        self._waiting_images = 0
        self._queued_workflow_bytes = 0
        self._active_comfy_controls = 0
        self._fatal_error: str | None = None
        self._last_error: str | None = None
        self._active_prompt_id: str | None = None
        self._owner = GpuOwner.UNKNOWN
        self._state = HandoffState.INITIALIZING
        self._state_changed_at = time.monotonic()
        self._closing = False
        self._dispatcher: asyncio.Task[None] | None = None
        self._idle_restore_deadline: float | None = None

    def _set_state(self, state: HandoffState) -> None:
        if state == self._state:
            return
        previous = self._state
        self._state = state
        self._state_changed_at = time.monotonic()
        LOG.info("state transition: %s -> %s", previous, state)

    def _state_stall_timeout(self) -> float | None:
        return {
            HandoffState.VERIFYING_LLM: self._config.reload_timeout,
            HandoffState.DRAINING_LLM: self._config.chat_drain_timeout,
            HandoffState.UNLOADING_LLM: self._config.unload_timeout,
            HandoffState.IMAGE_ACTIVE: (
                self._config.image_timeout + self._config.request_timeout
            ),
            HandoffState.CLEANING_COMFY: self._config.cleanup_timeout * 2,
            HandoffState.RELOADING_LLM: self._config.reload_timeout,
        }.get(self._state)

    def status(self) -> dict[str, Any]:
        state_age = max(0.0, time.monotonic() - self._state_changed_at)
        stall_timeout = self._state_stall_timeout()
        state_stalled = stall_timeout is not None and state_age > stall_timeout + 5
        dispatcher_alive = self._dispatcher is not None and not self._dispatcher.done()
        healthy = (
            not self._fatal_error
            and not self._closing
            and dispatcher_alive
            and not state_stalled
        )
        owner = None if self._owner is GpuOwner.UNKNOWN else self._owner
        return {
            "state": self._state,
            "state_age_seconds": round(state_age, 3),
            "state_stalled": state_stalled,
            "healthy": healthy,
            "dispatcher_alive": dispatcher_alive,
            "gpu_owner": owner,
            "active_chats": self._active_chats,
            "waiting_chats": self._waiting_chats,
            "waiting_images": self._waiting_images,
            "queued_workflow_bytes": self._queued_workflow_bytes,
            "max_queued_images": self._config.max_queued_images,
            "max_queued_workflow_bytes": self._config.max_queued_workflow_bytes,
            "active_comfy_controls": self._active_comfy_controls,
            "active_prompt_id": self._active_prompt_id,
            "last_error": self._last_error,
            "chat_available": not self._fatal_error and not self._closing,
            "llm_backend": self._llm.info.kind,
            "idle_timeout": self._config.idle_timeout,
            "idle_restore_enabled": self._config.restore_llm_on_idle,
            "idle_restore_scheduled": self._idle_restore_deadline is not None,
            "comfy_route_policy": (
                "transparent" if self._config.allow_unknown_comfy_routes else "strict"
            ),
        }

    async def initialize(self) -> bool:
        """Give startup GPU ownership to the configured LLM before accepting work."""
        LOG.info("broker initialization started")
        try:
            await self._llm.validate_control()
            self._set_state(HandoffState.CLEANING_COMFY)
            await self._comfy.free_models()
            self._set_state(HandoffState.VERIFYING_LLM)
            self._restore_point = await self._llm.snapshot_ready()
        except Exception as exc:
            LOG.error("broker initialization failed: %s", exc)
            async with self._condition:
                self._last_error = str(exc)
                self._fatal_error = "broker initialization failed"
                self._owner = GpuOwner.UNKNOWN
                self._set_state(HandoffState.ERROR)
                self._condition.notify_all()
            return False
        async with self._condition:
            self._last_error = None
            self._fatal_error = None
            self._owner = GpuOwner.LLM
            self._set_state(HandoffState.LLM_READY)
            self._dispatcher = asyncio.create_task(
                self._dispatch(),
                name="st-proxy-fifo-dispatcher",
            )
            self._condition.notify_all()
        LOG.info(
            "broker initialization completed: GPU owner=%s idle_timeout=%.1fs",
            self._llm.info.label,
            self._config.idle_timeout,
        )
        return True

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def _append_queued(self, work: QueuedWork) -> None:
        self._queue.append(work)
        if isinstance(work, ChatWork):
            self._waiting_chats += 1
        else:
            self._waiting_images += 1
            self._queued_workflow_bytes += len(work.body)

    def _release_queue_accounting(self, work: QueuedWork) -> None:
        if isinstance(work, ChatWork):
            self._waiting_chats -= 1
        else:
            self._waiting_images -= 1
            self._queued_workflow_bytes -= len(work.body)

    def _remove_queued(self, work: QueuedWork) -> bool:
        try:
            self._queue.remove(work)
        except ValueError:
            return False
        self._release_queue_accounting(work)
        return True

    @contextlib.asynccontextmanager
    async def chat_lease(self) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        async with self._condition:
            if self._closing:
                raise ChatUnavailable("broker is shutting down")
            if self._fatal_error:
                raise ChatUnavailable(self._fatal_error)
            work = ChatWork(self._next_sequence(), loop.create_future())
            self._cancel_idle_restore(f"chat request arrived: sequence={work.sequence}")
            self._append_queued(work)
            LOG.info(
                "chat request queued: sequence=%s waiting_chats=%s",
                work.sequence,
                self._waiting_chats,
            )
            self._condition.notify_all()

        entered = False
        try:
            error = await asyncio.shield(work.ready)
            if error:
                raise ChatUnavailable(error)
            entered = True
            yield
        finally:
            async with self._condition:
                if not entered:
                    work.cancelled = True
                    self._remove_queued(work)
                if work.granted:
                    work.granted = False
                    self._active_chats -= 1
                    LOG.debug("chat lease released: active_chats=%s", self._active_chats)
                self._condition.notify_all()

    async def submit_image(
        self,
        *,
        path: str,
        query_string: str,
        headers: dict[str, str],
        body: bytes,
    ) -> BufferedResponse:
        loop = asyncio.get_running_loop()
        async with self._condition:
            if self._closing:
                raise HandoffError("broker is shutting down")
            if self._fatal_error:
                raise HandoffError(self._fatal_error)
            body_size = len(body)
            if body_size > self._config.max_workflow_body_bytes:
                raise HandoffError(
                    "workflow request body exceeds the configured size limit"
                )
            if self._waiting_images >= self._config.max_queued_images:
                raise QueueFull("workflow queue has reached its request limit")
            if (
                self._queued_workflow_bytes + body_size
                > self._config.max_queued_workflow_bytes
            ):
                raise QueueFull("workflow queue has reached its memory limit")
            work = ImageWork(
                self._next_sequence(),
                path,
                query_string,
                headers,
                body,
                loop.create_future(),
            )
            self._cancel_idle_restore(f"image request arrived: sequence={work.sequence}")
            self._append_queued(work)
            LOG.info(
                "image request queued: sequence=%s waiting_images=%s",
                work.sequence,
                self._waiting_images,
            )
            self._condition.notify_all()

        completed = False
        try:
            outcome = await asyncio.shield(work.submitted)
            completed = True
        finally:
            if not completed:
                async with self._condition:
                    work.cancelled = True
                    removed = self._remove_queued(work)
                    if removed:
                        LOG.info(
                            "cancelled image request removed before submission: sequence=%s",
                            work.sequence,
                        )
                    self._condition.notify_all()
        if outcome.response is None:
            raise HandoffError(outcome.error or "image handoff failed before submission")
        return outcome.response

    @contextlib.asynccontextmanager
    async def comfy_control_lease(self) -> AsyncIterator[None]:
        async with self._condition:
            if self._closing:
                raise HandoffError("broker is shutting down")
            if self._fatal_error:
                raise HandoffError(self._fatal_error)
            if self._owner is not GpuOwner.COMFY or self._state not in {
                HandoffState.COMFY_READY,
                HandoffState.IMAGE_ACTIVE,
            }:
                raise HandoffError("ComfyUI does not currently own the GPU")
            self._cancel_idle_restore("ComfyUI control request arrived")
            self._active_comfy_controls += 1
            LOG.debug(
                "ComfyUI control lease granted: active_controls=%s",
                self._active_comfy_controls,
            )

        try:
            yield
        finally:
            async with self._condition:
                self._active_comfy_controls -= 1
                LOG.debug(
                    "ComfyUI control lease released: active_controls=%s",
                    self._active_comfy_controls,
                )
                self._condition.notify_all()

    def _idle_restore_is_safe(self) -> bool:
        return (
            self._config.restore_llm_on_idle
            and self._owner is GpuOwner.COMFY
            and self._state is HandoffState.COMFY_READY
            and not self._queue
            and not self._active_chats
            and not self._active_comfy_controls
            and self._active_prompt_id is None
            and not self._closing
            and not self._fatal_error
        )

    def _cancel_idle_restore(self, reason: str) -> None:
        if self._idle_restore_deadline is None:
            return
        self._idle_restore_deadline = None
        LOG.info("ComfyUI idle restore timer cancelled: %s", reason)

    async def _next_work(self) -> DispatchItem | None:
        async with self._condition:
            while not self._queue and not self._closing and not self._fatal_error:
                if not self._idle_restore_is_safe():
                    self._cancel_idle_restore("GPU ownership or lifecycle state changed")
                    await self._condition.wait()
                    continue
                loop = asyncio.get_running_loop()
                if self._idle_restore_deadline is None:
                    self._idle_restore_deadline = loop.time() + self._config.idle_timeout
                    LOG.info(
                        "ComfyUI idle restore timer started: timeout=%.1fs",
                        self._config.idle_timeout,
                    )
                remaining = self._idle_restore_deadline - loop.time()
                if remaining <= 0:
                    self._idle_restore_deadline = None
                    return _DispatchSignal.IDLE_RESTORE
                try:
                    async with asyncio.timeout(remaining):
                        await self._condition.wait()
                except TimeoutError:
                    pass
            if self._closing or self._fatal_error:
                return None
            self._cancel_idle_restore("queued work is ready to dispatch")
            work = self._queue.popleft()
            self._release_queue_accounting(work)
            return work

    async def _dispatch(self) -> None:
        current: DispatchItem | None = None
        try:
            while (current := await self._next_work()) is not None:
                if isinstance(current, ChatWork):
                    await self._dispatch_chat(current)
                elif isinstance(current, ImageWork):
                    await self._dispatch_image(current)
                else:
                    await self._dispatch_idle_restore()
                current = None
        except asyncio.CancelledError:
            if isinstance(current, ChatWork):
                self._resolve_chat(current, "broker is shutting down")
            elif isinstance(current, ImageWork):
                self._resolve_image_error(current, "broker is shutting down")
            await self._restore_for_shutdown()
            raise
        except Exception as exc:
            LOG.exception("FIFO dispatcher failed")
            error = f"broker dispatcher failed: {type(exc).__name__}"
            self._last_error = str(exc)
            if isinstance(current, ChatWork):
                self._resolve_chat(current, error)
            elif isinstance(current, ImageWork):
                self._resolve_image_error(current, error)
            await self._mark_fatal(error)

    async def _dispatch_idle_restore(self) -> None:
        async with self._condition:
            if not self._idle_restore_is_safe():
                LOG.info("ComfyUI idle restore skipped: work or lifecycle state changed")
                return
            self._owner = GpuOwner.UNKNOWN
            self._set_state(HandoffState.CLEANING_COMFY)

        LOG.info(
            "ComfyUI idle timeout reached: restoring GPU ownership to %s",
            self._llm.info.label,
        )
        errors: list[str] = []
        restored = await self._restore_llm(errors)
        self._record_errors(errors)
        if restored:
            LOG.info(
                "ComfyUI idle restore completed: owner=%s",
                self._llm.info.label,
            )
        else:
            LOG.error(
                "ComfyUI idle restore failed: %s is not confirmed ready",
                self._llm.info.label,
            )

    async def _dispatch_chat(self, work: ChatWork) -> None:
        if work.cancelled:
            return
        errors: list[str] = []
        if self._owner is not GpuOwner.LLM:
            restored = await self._restore_llm(errors)
            self._record_errors(errors)
            if not restored:
                self._resolve_chat(
                    work,
                    self._fatal_error
                    or f"{self._llm.info.label} is not confirmed ready",
                )
                return

        async with self._condition:
            if work.cancelled:
                return
            if self._closing:
                self._resolve_chat(work, "broker is shutting down")
                return
            if self._fatal_error:
                self._resolve_chat(work, self._fatal_error)
                return
            work.granted = True
            self._active_chats += 1
            LOG.debug(
                "chat lease granted: sequence=%s active_chats=%s",
                work.sequence,
                self._active_chats,
            )
            self._resolve_chat(work, None)

    async def _dispatch_image(self, work: ImageWork) -> None:
        errors: list[str] = []
        self._last_error = None
        try:
            if work.cancelled:
                self._resolve_image_error(work, "image request was cancelled")
                return
            if self._owner is not GpuOwner.COMFY:
                activated = await self._activate_comfy(work)
                if not activated:
                    self._resolve_image_error(work, "image request was cancelled")
                    return
            if work.cancelled:
                self._resolve_image_error(work, "image request was cancelled")
                return
            self._set_state(HandoffState.IMAGE_ACTIVE)
            try:
                result = await self._comfy.submit(
                    path=work.path,
                    query_string=work.query_string,
                    headers=work.headers,
                    body=work.body,
                )
            finally:
                work.body = b""
                work.headers.clear()
            if not work.submitted.done():
                work.submitted.set_result(SubmissionOutcome(response=result.response))
            if result.response.status >= 400:
                raise HandoffError(f"ComfyUI rejected prompt with HTTP {result.response.status}")
            if not result.prompt_id:
                raise HandoffError("ComfyUI response did not contain a prompt_id")
            self._active_prompt_id = result.prompt_id
            LOG.info("ComfyUI image job started: prompt_id=%s", result.prompt_id)
            await self._comfy.wait_for_prompt(result.prompt_id)
            LOG.info("ComfyUI image job completed: prompt_id=%s", result.prompt_id)
            self._set_state(HandoffState.COMFY_READY)
        except asyncio.CancelledError:
            LOG.warning("image handoff cancelled during shutdown")
            errors.append("handoff cancelled during shutdown")
            if self._active_prompt_id:
                await self._comfy.interrupt()
            self._resolve_image_error(work, "broker is shutting down")
            raise
        except Exception as exc:
            LOG.warning("image handoff failed: %s", exc)
            error = str(exc) or type(exc).__name__
            errors.append(error)
            if self._owner is not GpuOwner.LLM and not self._closing:
                await self._restore_llm(errors)
            elif self._owner is GpuOwner.LLM and not self._fatal_error:
                self._set_state(HandoffState.LLM_READY)
            self._resolve_image_error(work, error)
        finally:
            self._active_prompt_id = None
            self._record_errors(errors)

    async def _activate_comfy(self, work: ImageWork) -> bool:
        async with self._condition:
            self._set_state(HandoffState.DRAINING_LLM)
            LOG.info("draining chat requests: active_chats=%s", self._active_chats)
            async with asyncio.timeout(self._config.chat_drain_timeout):
                while self._active_chats:
                    if work.cancelled:
                        self._set_state(HandoffState.LLM_READY)
                        LOG.info(
                            "cancelled image request stopped before GPU handoff: sequence=%s",
                            work.sequence,
                        )
                        return False
                    await self._condition.wait()
            if work.cancelled:
                self._set_state(HandoffState.LLM_READY)
                return False
            LOG.info("chat requests drained")

        self._owner = GpuOwner.UNKNOWN
        self._set_state(HandoffState.UNLOADING_LLM)
        if self._restore_point is None:
            raise HandoffError("LLM restore point is unavailable")
        await self._llm.release_gpu(self._restore_point)
        self._owner = GpuOwner.COMFY
        self._set_state(HandoffState.COMFY_READY)
        LOG.info("GPU ownership transferred: owner=ComfyUI")
        return True

    async def _restore_llm(self, errors: list[str]) -> bool:
        if self._owner is GpuOwner.LLM:
            return True

        try:
            async with self._condition:
                self._owner = GpuOwner.UNKNOWN
                self._set_state(HandoffState.CLEANING_COMFY)
                async with asyncio.timeout(self._config.cleanup_timeout):
                    while self._active_comfy_controls:
                        await self._condition.wait()
        except TimeoutError:
            error = "timed out waiting for ComfyUI control requests to finish"
            LOG.error(error)
            errors.append(error)
            self._record_errors(errors)
            if not self._closing:
                await self._mark_fatal("ComfyUI GPU release is not confirmed")
            return False

        try:
            async with asyncio.timeout(self._config.cleanup_timeout):
                await self._comfy.free_models()
        except Exception as exc:
            LOG.error("ComfyUI cleanup failed; GPU release is unconfirmed: %s", exc)
            errors.append(str(exc))
            self._record_errors(errors)
            if not self._closing:
                await self._mark_fatal("ComfyUI GPU release is not confirmed")
            return False

        self._set_state(HandoffState.RELOADING_LLM)
        try:
            if self._restore_point is None:
                raise HandoffError("LLM restore point is unavailable")
            await self._llm.acquire_gpu(self._restore_point)
        except Exception as exc:
            LOG.error("%s reload failed: %s", self._llm.info.label, exc)
            errors.append(str(exc))
            self._record_errors(errors)
            if not self._closing:
                await self._mark_fatal(
                    f"{self._llm.info.label} is not confirmed ready"
                )
            return False

        self._owner = GpuOwner.LLM
        if not self._closing:
            self._fatal_error = None
            self._set_state(HandoffState.LLM_READY)
            LOG.info(
                "GPU ownership transferred: owner=%s",
                self._llm.info.label,
            )
        return True

    def _record_errors(self, errors: list[str]) -> None:
        if errors:
            self._last_error = "; ".join(dict.fromkeys(errors))

    def _resolve_chat(self, work: ChatWork, error: str | None) -> None:
        if not work.ready.done():
            work.ready.set_result(error)

    def _resolve_image_error(self, work: ImageWork, error: str) -> None:
        if not work.submitted.done():
            work.submitted.set_result(SubmissionOutcome(error=error))

    async def _mark_fatal(self, error: str) -> None:
        async with self._condition:
            self._cancel_idle_restore("broker entered a fatal error state")
            self._fatal_error = error
            self._set_state(HandoffState.ERROR)
            self._fail_queued(error)
            self._condition.notify_all()

    def _fail_queued(self, error: str) -> None:
        while self._queue:
            work = self._queue.popleft()
            self._release_queue_accounting(work)
            if isinstance(work, ChatWork):
                self._resolve_chat(work, error)
            else:
                self._resolve_image_error(work, error)

    async def _restore_for_shutdown(self) -> None:
        errors: list[str] = []
        if self._owner is not GpuOwner.LLM:
            restore = asyncio.create_task(
                self._restore_llm(errors),
                name="st-proxy-shutdown-restore",
            )
            try:
                await asyncio.shield(restore)
            except asyncio.CancelledError:
                await restore
        self._record_errors(errors)
        self._set_state(HandoffState.SHUTTING_DOWN)

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._set_state(HandoffState.SHUTTING_DOWN)
        LOG.info("handoff coordinator shutdown started")
        async with self._condition:
            self._cancel_idle_restore("broker is shutting down")
            self._fail_queued("broker is shutting down")
            self._condition.notify_all()
        if self._dispatcher is not None:
            self._dispatcher.cancel()
            await asyncio.gather(self._dispatcher, return_exceptions=True)
            self._dispatcher = None
        LOG.info("handoff coordinator shutdown completed")
