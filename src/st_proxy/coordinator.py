from __future__ import annotations

import asyncio
import contextlib
import logging
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
    IMAGE_ACTIVE = "image_active"
    CLEANING_COMFY = "cleaning_comfy"
    RELOADING_LLM = "reloading_llm"
    ERROR = "error"
    SHUTTING_DOWN = "shutting_down"


@dataclass(slots=True)
class SubmissionOutcome:
    response: BufferedResponse | None = None
    error: str | None = None


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
        self._handoff_lock = asyncio.Lock()
        self._active_chats = 0
        self._waiting_chats = 0
        self._waiting_images = 0
        self._accept_chats = False
        self._fatal_error: str | None = None
        self._last_error: str | None = None
        self._active_prompt_id: str | None = None
        self._state = HandoffState.INITIALIZING
        self._closing = False
        self._tasks: set[asyncio.Task[None]] = set()

    def _set_state(self, state: HandoffState) -> None:
        if state == self._state:
            return
        previous = self._state
        self._state = state
        LOG.info("state transition: %s -> %s", previous, state)

    def status(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "active_chats": self._active_chats,
            "waiting_chats": self._waiting_chats,
            "waiting_images": self._waiting_images,
            "active_prompt_id": self._active_prompt_id,
            "last_error": self._last_error,
            "chat_available": self._accept_chats and not self._fatal_error and not self._closing,
        }

    async def initialize(self) -> bool:
        """Give startup GPU ownership to KoboldCpp before accepting chat."""
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
                self._accept_chats = False
                self._set_state(HandoffState.ERROR)
                self._condition.notify_all()
            return False
        async with self._condition:
            self._last_error = None
            self._fatal_error = None
            self._accept_chats = True
            self._set_state(HandoffState.LLM_READY)
            self._condition.notify_all()
        LOG.info("broker initialization completed: GPU owner=KoboldCpp")
        return True

    @contextlib.asynccontextmanager
    async def chat_lease(self) -> AsyncIterator[None]:
        async with self._condition:
            self._waiting_chats += 1
            try:
                while not self._accept_chats and not self._fatal_error and not self._closing:
                    await self._condition.wait()
                if self._closing:
                    raise ChatUnavailable("broker is shutting down")
                if self._fatal_error:
                    raise ChatUnavailable(self._fatal_error)
                self._active_chats += 1
                LOG.debug("chat lease acquired: active_chats=%s", self._active_chats)
            finally:
                self._waiting_chats -= 1
        try:
            yield
        finally:
            async with self._condition:
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
        if self._closing:
            raise HandoffError("broker is shutting down")
        loop = asyncio.get_running_loop()
        submitted: asyncio.Future[SubmissionOutcome] = loop.create_future()
        task = asyncio.create_task(
            self._handoff(path, query_string, headers, body, submitted),
            name="st-proxy-image-handoff",
        )
        LOG.info("image handoff queued: waiting_images=%s", self._waiting_images + 1)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        outcome = await asyncio.shield(submitted)
        if outcome.response is None:
            raise HandoffError(outcome.error or "image handoff failed before submission")
        return outcome.response

    async def _drain_chats(self) -> None:
        async with self._condition:
            self._accept_chats = False
            self._set_state(HandoffState.DRAINING_LLM)
            LOG.info("draining chat requests: active_chats=%s", self._active_chats)
            self._condition.notify_all()
            async with asyncio.timeout(self._config.chat_drain_timeout):
                while self._active_chats:
                    await self._condition.wait()
            LOG.info("chat requests drained")

    async def _handoff(
        self,
        path: str,
        query_string: str,
        headers: dict[str, str],
        body: bytes,
        submitted: asyncio.Future[SubmissionOutcome],
    ) -> None:
        self._waiting_images += 1
        queued = True
        errors: list[str] = []
        reloaded = False
        restore_needed = False
        try:
            async with self._handoff_lock:
                self._waiting_images -= 1
                queued = False
                self._last_error = None
                try:
                    await self._drain_chats()
                    # From this point onward an Admin operation may have changed
                    # KoboldCpp, so every exit path must restore its startup model.
                    restore_needed = True
                    self._set_state(HandoffState.UNLOADING_LLM)
                    await self._kobold.unload()
                    self._set_state(HandoffState.IMAGE_ACTIVE)
                    LOG.info("GPU ownership transferred: owner=ComfyUI")
                    result = await self._comfy.submit(
                        path=path,
                        query_string=query_string,
                        headers=headers,
                        body=body,
                    )
                    if not submitted.done():
                        submitted.set_result(SubmissionOutcome(response=result.response))
                    if result.response.status >= 400:
                        raise HandoffError(
                            f"ComfyUI rejected prompt with HTTP {result.response.status}"
                        )
                    if not result.prompt_id:
                        raise HandoffError("ComfyUI response did not contain a prompt_id")
                    self._active_prompt_id = result.prompt_id
                    LOG.info("ComfyUI image job started: prompt_id=%s", result.prompt_id)
                    await self._comfy.wait_for_prompt(result.prompt_id)
                    LOG.info("ComfyUI image job completed: prompt_id=%s", result.prompt_id)
                except asyncio.CancelledError:
                    LOG.warning("image handoff cancelled during shutdown")
                    errors.append("handoff cancelled during shutdown")
                    if restore_needed:
                        await self._comfy.interrupt()
                    raise
                except Exception as exc:
                    LOG.warning("image handoff failed: %s", exc)
                    errors.append(str(exc))
                finally:
                    self._active_prompt_id = None
                    cancelled_during_restore = False
                    if restore_needed:
                        restore_task = asyncio.create_task(
                            self._cleanup_and_reload(errors),
                            name="st-proxy-restore-kobold",
                        )
                        try:
                            reloaded = await asyncio.shield(restore_task)
                        except asyncio.CancelledError:
                            # Shutdown must not interrupt the operation that gives the GPU
                            # back to KoboldCpp. Wait for it, then propagate cancellation.
                            cancelled_during_restore = True
                            reloaded = await restore_task
                    else:
                        # Draining timed out or was cancelled before any Admin call.
                        # The existing LLM remains authoritative and must not be restarted
                        # out from under its still-active request.
                        reloaded = True
                    async with self._condition:
                        if reloaded and not self._closing:
                            self._fatal_error = None
                            self._accept_chats = True
                            self._set_state(HandoffState.LLM_READY)
                            LOG.info("GPU ownership transferred: owner=KoboldCpp")
                        else:
                            self._accept_chats = False
                            self._fatal_error = "KoboldCpp is not confirmed ready"
                            self._set_state(
                                HandoffState.SHUTTING_DOWN if self._closing else HandoffState.ERROR
                            )
                        self._condition.notify_all()
                    if cancelled_during_restore:
                        raise asyncio.CancelledError
        finally:
            if queued:
                # Normally decremented after acquiring the lock; this covers cancellation in queue.
                self._waiting_images -= 1
            if errors:
                self._last_error = "; ".join(dict.fromkeys(errors))
            if not submitted.done():
                submitted.set_result(SubmissionOutcome(error=self._last_error or "handoff stopped"))

    async def _cleanup_and_reload(self, errors: list[str]) -> bool:
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
            return False
        return True

    async def close(self) -> None:
        self._closing = True
        self._set_state(HandoffState.SHUTTING_DOWN)
        LOG.info("handoff coordinator shutdown started")
        async with self._condition:
            self._accept_chats = False
            self._condition.notify_all()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        LOG.info("handoff coordinator shutdown completed")
