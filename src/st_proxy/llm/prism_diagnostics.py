"""Bounded, content-free observation of Prism's Chat Completions responses."""
from __future__ import annotations

import json
import logging
from typing import Any

LOG = logging.getLogger(__name__)
MAX_EVENT_BYTES = 1024 * 1024
UNKNOWN = "unavailable"


def request_usage(payload: dict[str, Any]) -> bytes:
    """Request streamed usage without replacing other stream options or invalid inputs."""
    if payload.get("stream") is True:
        options = payload.get("stream_options")
        if options is None or isinstance(options, dict):
            payload = {**payload, "stream_options": {**(options or {}), "include_usage": True}}
    return json.dumps(payload, ensure_ascii=False).encode()


def _count(value: Any) -> int | str:
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else UNKNOWN


class PrismDiagnostics:
    def __init__(self, body: bytes) -> None:
        payload = json.loads(body)
        self.max_tokens = _count(payload.get("max_tokens"))
        effort = payload.get("reasoning_effort")
        self.effort = effort if isinstance(effort, str) and effort in {
            "none", "minimal", "low", "medium", "high", "xhigh", "unset",
        } else UNKNOWN
        kwargs = payload.get("chat_template_kwargs")
        enabled = kwargs.get("enable_thinking") if isinstance(kwargs, dict) else None
        self.enabled = str(enabled).lower() if type(enabled) is bool else UNKNOWN
        self.status: int | None = None
        self.sse = False
        self.valid = True
        self.done = False
        self.buffer = bytearray()
        self.data: list[bytes] = []
        self.data_size = 0
        self.reasoning_chars = 0
        self.finish_reasons: set[str] = set()
        self.usage: dict[str, int | str] = {}

    def start(self, status: int, content_type: str) -> None:
        self.status = status
        self.sse = content_type == "text/event-stream"
        self.valid = status == 200 and content_type in {"text/event-stream", "application/json"}

    def _invalidate(self) -> None:
        self.valid = False
        self.buffer.clear()
        self.data.clear()
        self.data_size = 0

    def feed(self, chunk: bytes) -> None:
        if not self.valid:
            return
        # Split before buffering so one network chunk can contain many small events.
        pieces = chunk.splitlines(keepends=True) if self.sse else [chunk]
        for piece in pieces:
            if len(self.buffer) + self.data_size + len(piece) > MAX_EVENT_BYTES:
                self._invalidate()
                return
            self.buffer.extend(piece)
            if not self.sse or not self.buffer.endswith(b"\n"):
                continue
            line = bytes(self.buffer).rstrip(b"\r\n")
            self.buffer.clear()
            if line.startswith(b"data:"):
                value = line[5:].removeprefix(b" ")
                self.data.append(value)
                self.data_size += len(value) + 1
            elif not line and self.data:
                event = b"\n".join(self.data)
                self.data.clear()
                self.data_size = 0
                if event.strip() == b"[DONE]":
                    self.done = True
                else:
                    self._event(event)
                if not self.valid:
                    return

    def _event(self, raw: bytes) -> None:
        try:
            event = json.loads(raw)
            if not isinstance(event, dict) or "error" in event:
                raise ValueError
            choices = event.get("choices", [])
            if not isinstance(choices, list):
                raise ValueError
            for choice in choices:
                if not isinstance(choice, dict):
                    raise ValueError
                reason = choice.get("finish_reason")
                if reason is not None:
                    self.finish_reasons.add(reason if isinstance(reason, str) and reason in {
                        "stop", "length", "tool_calls", "function_call", "content_filter",
                    } else "other")
                message = choice.get("delta" if self.sse else "message", {})
                if not isinstance(message, dict):
                    raise ValueError
                thinking = message.get("reasoning_content")
                if isinstance(thinking, str):
                    self.reasoning_chars += len(thinking)
            usage = event.get("usage")
            if isinstance(usage, dict):
                for key in ("prompt_tokens", "completion_tokens"):
                    self.usage[key] = _count(usage.get(key))
                details = usage.get("prompt_tokens_details")
                self.usage["cached_tokens"] = _count(
                    details.get("cached_tokens") if isinstance(details, dict) else None)
                details = usage.get("completion_tokens_details")
                self.usage["reasoning_tokens"] = _count(
                    details.get("reasoning_tokens") if isinstance(details, dict) else None)
        except (ValueError, UnicodeError, RecursionError):
            self._invalidate()

    def finish(self, *, upstream_complete: bool, client_connected: bool) -> None:
        if self.valid and not self.sse and upstream_complete:
            self._event(bytes(self.buffer))
        if self.sse and (self.buffer or self.data):
            self.valid = False
        if self.status is not None and self.status != 200:
            stream_status = "http_error"
        elif not upstream_complete:
            stream_status = "interrupted"
        elif not self.valid or not self.finish_reasons or (self.sse and not self.done):
            stream_status = "incomplete"
        else:
            stream_status = "complete"
        LOG.info(
            "Prism response: max_tokens=%s reasoning_effort=%s enable_thinking=%s "
            "prompt_tokens=%s cached_tokens=%s completion_tokens=%s reasoning_tokens=%s "
            "reasoning_chars=%s finish_reason=%s stream_status=%s client_disconnected=%s",
            self.max_tokens, self.effort, self.enabled,
            *(self.usage.get(key, UNKNOWN) for key in (
                "prompt_tokens", "cached_tokens", "completion_tokens", "reasoning_tokens")),
            self.reasoning_chars if self.valid else UNKNOWN,
            ",".join(sorted(self.finish_reasons)) or UNKNOWN,
            stream_status, str(not client_connected).lower(),
        )
        self.buffer.clear()
        self.data.clear()
