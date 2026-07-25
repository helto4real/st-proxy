from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .clients import ComfyClient, KoboldClient
from .config import BrokerConfig
from .errors import ChatUnavailable, HandoffError
from .http import BufferedResponse

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


QueuedWork = ChatWork | ImageWork


class HandoffCoordinator:
    def __init__(
        self,
        config: BrokerConfig,
        kobold: KoboldClient,
        comfy: ComfyClient,
    ) -> None:
        self._config = config
        self._kobold = kobold
        self._comfy = comfy
        self._condition = asyncio.Condition()
        self._queue: deque[QueuedWork] = deque()
        self._sequence = 0
        self._active_chats = 0
        self._waiting_chats = 0
        self._waiting_images = 0
        self._fatal_error: str | None = None
        self._last_error: str | None = None
        self._active_prompt_id: str | None = None
        self._owner = GpuOwner.UNKNOWN
        self._state = HandoffState.INITIALIZING
        self._closing = False
        self._dispatcher: asyncio.Task[None] | None = None

    def _set_state(self, state: HandoffState) -> None:
        if state == self._state:
            return
        previous = self._state
        self._state = state
        LOG.info("state transition: %s -> %s", previous, state)

    def status(self) -> dict[str, Any]:
        owner = None if self._owner is GpuOwner.UNKNOWN else self._owner
        return {
            "state": self._state,
            "gpu_owner": owner,
            "active_chats": self._active_chats,
            "waiting_chats": self._waiting_chats,
            "waiting_images": self._waiting_images,
            "active_prompt_id": self._active_prompt_id,
            "last_error": self._last_error,
            "chat_available": not self._fatal_error and not self._closing,
        }

    async def initialize(self) -> bool:
        """Give startup GPU ownership to KoboldCpp before accepting work."""
        LOG.info("broker initialization started")
        try:
            await self._kobold.ensure_admin_ready()
            self._set_state(HandoffState.CLEANING_COMFY)
            await self._comfy.free_models()
            self._set_state(HandoffState.VERIFYING_LLM)
            await self._kobold.ensure_loaded()
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
        LOG.info("broker initialization completed: GPU owner=KoboldCpp")
        return True

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    @contextlib.asynccontextmanager
    async def chat_lease(self) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        async with self._condition:
            if self._closing:
                raise ChatUnavailable("broker is shutting down")
            if self._fatal_error:
                raise ChatUnavailable(self._fatal_error)
            work = ChatWork(self._next_sequence(), loop.create_future())
            self._queue.append(work)
            self._waiting_chats += 1
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
                    try:
                        self._queue.remove(work)
                    except ValueError:
                        pass
                    else:
                        self._waiting_chats -= 1
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
            work = ImageWork(
                self._next_sequence(),
                path,
                query_string,
                headers,
                body,
                loop.create_future(),
            )
            self._queue.append(work)
            self._waiting_images += 1
            LOG.info(
                "image request queued: sequence=%s waiting_images=%s",
                work.sequence,
                self._waiting_images,
            )
            self._condition.notify_all()

        outcome = await asyncio.shield(work.submitted)
        if outcome.response is None:
            raise HandoffError(outcome.error or "image handoff failed before submission")
        return outcome.response

    async def _next_work(self) -> QueuedWork | None:
        async with self._condition:
            while not self._queue and not self._closing and not self._fatal_error:
                await self._condition.wait()
            if self._closing or self._fatal_error:
                return None
            work = self._queue.popleft()
            if isinstance(work, ChatWork):
                self._waiting_chats -= 1
            else:
                self._waiting_images -= 1
            return work

    async def _dispatch(self) -> None:
        current: QueuedWork | None = None
        try:
            while current := await self._next_work():
                if isinstance(current, ChatWork):
                    await self._dispatch_chat(current)
                else:
                    await self._dispatch_image(current)
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

    async def _dispatch_chat(self, work: ChatWork) -> None:
        if work.cancelled:
            return
        errors: list[str] = []
        if self._owner is not GpuOwner.LLM:
            restored = await self._restore_llm(errors)
            self._record_errors(errors)
            if not restored:
                self._resolve_chat(work, self._fatal_error or "KoboldCpp is not confirmed ready")
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
            if self._owner is not GpuOwner.COMFY:
                await self._activate_comfy()
            self._set_state(HandoffState.IMAGE_ACTIVE)
            result = await self._comfy.submit(
                path=work.path,
                query_string=work.query_string,
                headers=work.headers,
                body=work.body,
            )
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

    async def _activate_comfy(self) -> None:
        async with self._condition:
            self._set_state(HandoffState.DRAINING_LLM)
            LOG.info("draining chat requests: active_chats=%s", self._active_chats)
            async with asyncio.timeout(self._config.chat_drain_timeout):
                while self._active_chats:
                    await self._condition.wait()
            LOG.info("chat requests drained")

        self._owner = GpuOwner.UNKNOWN
        self._set_state(HandoffState.UNLOADING_LLM)
        await self._kobold.unload()
        self._owner = GpuOwner.COMFY
        self._set_state(HandoffState.COMFY_READY)
        LOG.info("GPU ownership transferred: owner=ComfyUI")

    async def _restore_llm(self, errors: list[str]) -> bool:
        if self._owner is GpuOwner.LLM:
            return True

        self._owner = GpuOwner.UNKNOWN
        self._set_state(HandoffState.CLEANING_COMFY)
        try:
            async with asyncio.timeout(self._config.cleanup_timeout):
                await self._comfy.free_models()
        except Exception as exc:
            LOG.warning("ComfyUI cleanup failed: %s", exc)
            errors.append(str(exc))

        self._set_state(HandoffState.RELOADING_LLM)
        try:
            await self._kobold.reload_initial()
        except Exception as exc:
            LOG.error("KoboldCpp reload failed: %s", exc)
            errors.append(str(exc))
            self._record_errors(errors)
            if not self._closing:
                await self._mark_fatal("KoboldCpp is not confirmed ready")
            return False

        self._owner = GpuOwner.LLM
        if not self._closing:
            self._fatal_error = None
            self._set_state(HandoffState.LLM_READY)
            LOG.info("GPU ownership transferred: owner=KoboldCpp")
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
            self._fatal_error = error
            self._set_state(HandoffState.ERROR)
            self._fail_queued(error)
            self._condition.notify_all()

    def _fail_queued(self, error: str) -> None:
        while self._queue:
            work = self._queue.popleft()
            if isinstance(work, ChatWork):
                self._waiting_chats -= 1
                self._resolve_chat(work, error)
            else:
                self._waiting_images -= 1
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
            self._fail_queued("broker is shutting down")
            self._condition.notify_all()
        if self._dispatcher is not None:
            self._dispatcher.cancel()
            await asyncio.gather(self._dispatcher, return_exceptions=True)
            self._dispatcher = None
        LOG.info("handoff coordinator shutdown completed")
