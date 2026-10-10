from __future__ import annotations

import io
import logging
import re
import subprocess
import sys
from unittest.mock import patch

import pytest

from st_proxy.logging import LOG_FORMAT, CatppuccinFormatter, color_strata_line, configure_logging

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _record(message: str, level: int = logging.INFO, *args: object) -> logging.LogRecord:
    return logging.LogRecord("st_proxy.service", level, __file__, 1, message, args, None)


def test_terminal_highlights_preserve_text_and_other_handlers() -> None:
    record = _record(
        "request completed: target=%s method=POST path=/v1/chat/completions "
        "status=%s duration=%.3fs", logging.INFO, "TabbyAPI", 200, 1.234,
    )
    plain = logging.Formatter(LOG_FORMAT).format(record)
    colored = CatppuccinFormatter(color=True).format(record)

    assert "\x1b[38;2;137;180;250mINFO" in colored
    assert "\x1b[38;2;166;227;161mcompleted" in colored
    assert "target=\x1b[38;2;203;166;247mTabbyAPI" in colored
    assert "status=\x1b[38;2;166;227;161m200" in colored
    assert "duration=\x1b[38;2;148;226;213m1.234s" in colored
    assert ANSI.sub("", colored) == plain
    assert logging.Formatter(LOG_FORMAT).format(record) == plain
    assert record.levelname == "INFO"
    assert record.args == ("TabbyAPI", 200, 1.234)


@pytest.mark.parametrize(("status", "rgb"), [(403, "249;226;175"), (502, "243;139;168")])
def test_completed_http_errors_are_not_successes(status: int, rgb: str) -> None:
    record = _record(f"request completed: target=ComfyUI status={status} duration=0.100s")
    colored = CatppuccinFormatter(color=True).format(record)
    assert f"status=\x1b[38;2;{rgb}m{status}" in colored
    assert "\x1b[38;2;166;227;161mcompleted" not in colored


@pytest.mark.parametrize(("level", "rgb"), [
    (logging.DEBUG, "127;132;156"),
    (logging.WARNING, "249;226;175"),
    (logging.ERROR, "243;139;168"),
    (logging.CRITICAL, "243;139;168"),
])
def test_severity_labels(level: int, rgb: str) -> None:
    colored = CatppuccinFormatter(color=True).format(_record("message", level))
    assert f"\x1b[38;2;{rgb}m{logging.getLevelName(level)}" in colored


def test_failure_reason_stops_before_duration() -> None:
    record = _record(
        "request rejected: target=Qwen SC117 status=503 "
        "reason=backend is unavailable duration=0.001s", logging.ERROR,
    )
    colored = CatppuccinFormatter(color=True).format(record)
    assert "\x1b[38;2;203;166;247mQwen SC117" in colored
    assert "reason=\x1b[38;2;243;139;168mbackend is unavailable\x1b[38;2;205;214;244m " in colored
    assert "duration=\x1b[38;2;148;226;213m0.001s" in colored


def test_handoff_states_and_normal_queueing() -> None:
    formatter = CatppuccinFormatter(color=True)
    transition = formatter.format(_record("state transition: llm_ready -> unloading_llm"))
    assert "\x1b[38;2;250;179;135mllm_ready" in transition
    assert "\x1b[38;2;250;179;135munloading_llm" in transition
    error = formatter.format(_record("state transition: reloading_llm -> error"))
    assert "\x1b[38;2;243;139;168merror" in error
    queued = formatter.format(_record("chat request queued: sequence=1 waiting_chats=2"))
    assert "\x1b[38;2;249;226;175m" not in queued


@pytest.mark.parametrize("message", [
    "TabbyAPI reload failed: insufficient_vram",
    "TabbyAPI load error: insufficient_vram; waiting for load stream to end",
])
def test_lifecycle_failure_details(message: str) -> None:
    colored = CatppuccinFormatter(color=True).format(_record(message, logging.ERROR))
    assert "\x1b[38;2;243;139;168minsufficient_vram\x1b[38;2;205;214;244m" in colored


def test_exception_and_stack_text_survive_formatting() -> None:
    record = _record("synthetic failure", logging.ERROR)
    try:
        raise ValueError("synthetic detail")
    except ValueError:
        record.exc_info = sys.exc_info()
    record.stack_info = "Stack (most recent call last):\n  synthetic stack"
    colored = CatppuccinFormatter(color=True).format(record)
    assert record.exc_text is None
    plain = logging.Formatter(LOG_FORMAT).format(record)
    assert ANSI.sub("", colored) == plain
    assert "ValueError: synthetic detail" in colored


@pytest.mark.parametrize(("tty", "environment", "expected_color"), [
    (True, {}, True),
    (False, {}, False),
    (True, {"NO_COLOR": "1"}, False),
    (True, {"NO_COLOR": "0"}, False),
    (True, {"NO_COLOR": ""}, True),
    (True, {"TERM": "dumb"}, False),
])
def test_output_destination_and_environment(tty, environment, expected_color) -> None:
    stream = io.StringIO()
    with (
        patch("sys.stderr", stream),
        patch.object(stream, "isatty", return_value=tty),
        patch.dict("os.environ", environment, clear=True),
        patch("logging.basicConfig") as basic_config,
    ):
        configure_logging(logging.DEBUG)
    assert basic_config.call_args.kwargs["level"] == logging.DEBUG
    handler, = basic_config.call_args.kwargs["handlers"]
    record = _record("request completed: target=Prism status=200 duration=1.000s")
    handler.handle(record)
    output = stream.getvalue()
    assert ("\x1b[" in output) is expected_color
    assert ANSI.sub("", output) == logging.Formatter(LOG_FORMAT).format(record) + "\n"


@pytest.mark.parametrize(("message", "phase", "rgb"), [
    ("filling the GPU's expert cache (4486 experts, 7.29 GiB of VRAM) ...",
     "filling the GPU's expert cache", "250;179;135"),
    ("almost ready ...", "almost ready", "250;179;135"),
    ("still starting (34 s) - please wait ...", "still starting", "250;179;135"),
    ("the engine is running again", "the engine is running again", "166;227;161"),
    ("reading the prompt: 512 of 14,057 tokens, 2 s so far", "reading the prompt", "137;180;250"),
    ("answering: 9 of max 1500 tokens, 43.4 tok/s, 2 s", "answering", "148;226;213"),
    ("done: 38 tokens in 29 s (51.9 tok/s) (stop, cancel=False), expert cache 39.1% hit",
     "done", "166;227;161"),
    ("done: 38 tokens in 29 s (51.9 tok/s) (stop, cancel=True)", "done", "249;226;175"),
    ("done: 0 tokens in 1 s (0.0 tok/s) (error, cancel=False)", "done", "243;139;168"),
    ("error: synthetic failure", "error", "243;139;168"),
])
def test_strata_phases_and_metrics(message: str, phase: str, rgb: str) -> None:
    line = f"[strata] {message}\n"
    colored = color_strata_line(line)
    assert colored.startswith("\x1b[38;2;203;166;247m[strata]")
    assert f"\x1b[38;2;{rgb}m{phase}\x1b[38;2;205;214;244m" in colored
    assert ANSI.sub("", colored) == line
    for metric in ("14,057 tokens", "43.4 tok/s", "39.1%", "7.29 GiB"):
        if metric in message:
            assert f"\x1b[38;2;148;226;213m{metric}\x1b[38;2;205;214;244m" in colored


@pytest.mark.parametrize("line", [
    "unrelated child output\n", "\n", "  continuation\r\n",
    "\x1b[31m[strata] already colored\x1b[0m\n",
    "[strata] \x1b[31merror\x1b[0m\n",
])
def test_strata_filter_leaves_other_output_untouched(line: str) -> None:
    assert color_strata_line(line) == line


def test_strata_stream_filter_preserves_redirected_bytes() -> None:
    data = b"[strata] almost ready ...\r\nother: \xff\n\n[strata] done: 3 tokens"
    result = subprocess.run(
        [sys.executable, "-m", "st_proxy.strata_logging"],
        input=data, capture_output=True, check=True,
    )
    assert result.stdout == data
    assert result.stderr == b""
