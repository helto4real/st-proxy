from __future__ import annotations

import copy
import logging
import os
import re
from typing import TextIO

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# Catppuccin Mocha, using truecolor foregrounds only.
_GRAY = "\x1b[38;2;127;132;156m"
_TEXT = "\x1b[38;2;205;214;244m"
_BLUE = "\x1b[38;2;137;180;250m"
_GREEN = "\x1b[38;2;166;227;161m"
_YELLOW = "\x1b[38;2;249;226;175m"
_RED = "\x1b[38;2;243;139;168m"
_MAUVE = "\x1b[38;2;203;166;247m"
_TEAL = "\x1b[38;2;148;226;213m"
_PEACH = "\x1b[38;2;250;179;135m"
_RESET = "\x1b[0m"

_STATUS = re.compile(r"\bstatus=([1-5]\d{2})\b")
_TOKENS = re.compile(
    r"\b(?P<field>status|duration|state|expected|reason|error)="
    r"(?P<value>[^\n]*?)(?=\s+[a-zA-Z_]\w*=|[;\n]|$)"
    r"|(?P<transition>(?<=state transition: )\w+|(?<= -> )\w+)"
    r"|(?P<backend>\b(?:KoboldCpp|TabbyAPI|Ollama|Prism|ComfyUI|Qwen SC117|Qwen Orca)\b)"
    r"|(?P<completed>\bcompleted\b)"
    r"|(?P<failure>\bfailed\b|\brejected\b)"
    r"|(?P<detail>(?<=failed: )(?!\w+=)[^;\n]+|(?<=load error: )[^;\n]+)"
)

_STRATA_PHASES = {
    "starting the engine": _PEACH,
    "loading the experts": _PEACH,
    "filling the GPU's expert cache": _PEACH,
    "experts loaded": _PEACH,
    "almost ready": _PEACH,
    "still starting": _PEACH,
    "reading the prompt": _BLUE,
    "thinking": _BLUE,
    "answering": _TEAL,
    "the engine is running again": _GREEN,
    "done": _GREEN,
    "warning": _YELLOW,
    "error": _RED,
    "failed": _RED,
    "fatal": _RED,
}
_STRATA_TOKENS = re.compile(
    r"(?P<phase>^(?:" + "|".join(re.escape(phase) for phase in _STRATA_PHASES) + r")\b)"
    r"|(?P<cancel>cancel=True)"
    r"|(?P<number>\b\d[\d,]*(?:\.\d+)?(?:%|\s+(?:tok/s|tokens|experts|GiB|s)\b)?)"
)


def color_strata_line(line: str) -> str:
    """Highlight known Strata console fields without changing the text."""
    prefix = "[strata] "
    if not line.startswith(prefix) or "\x1b[" in line:
        return line
    body = line[len(prefix):].rstrip("\r\n")
    ending = line[len(prefix) + len(body):]

    def highlight(match: re.Match[str]) -> str:
        value = match[0]
        if match["phase"]:
            color = _STRATA_PHASES[value]
            if value == "done":
                if re.search(r"\((?:error|failed)\b", body):
                    color = _RED
                elif "cancel=True" in body:
                    color = _YELLOW
        else:
            color = _YELLOW if match["cancel"] else _TEAL
        return f"{color}{value}{_TEXT}"

    return f"{_MAUVE}[strata]{_TEXT} {_STRATA_TOKENS.sub(highlight, body)}{_RESET}{ending}"


def colors_enabled(stream: TextIO) -> bool:
    return (
        stream.isatty()
        and not os.environ.get("NO_COLOR")
        and os.environ.get("TERM") != "dumb"
    )


def _message_color(message: str, level: int) -> str:
    statuses = _STATUS.findall(message)
    successful = level < logging.WARNING and all(int(status) < 400 for status in statuses)

    def highlight(match: re.Match[str]) -> str:
        field = match["field"]
        value = match["value"] if field else match[0]
        color = None
        if field == "status":
            if re.fullmatch(r"[1-5]\d{2}", value):
                color = {"2": _GREEN, "4": _YELLOW, "5": _RED}.get(value[0])
        elif field == "duration":
            color = _TEAL
        elif field in {"state", "expected"} or match["transition"]:
            color = _RED if value == "error" else _PEACH
        elif field in {"reason", "error"} or match["detail"]:
            color = _RED
        elif match["backend"]:
            color = _MAUVE
        elif match["completed"] and successful:
            color = _GREEN
        elif match["failure"]:
            color = _RED if level >= logging.ERROR else _YELLOW
        if color is None:
            return match[0]
        prefix = f"{field}=" if field else ""
        return f"{prefix}{color}{value}{_TEXT}"

    return _TOKENS.sub(highlight, message)


class CatppuccinFormatter(logging.Formatter):
    def __init__(self, *, color: bool) -> None:
        super().__init__(LOG_FORMAT)
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        if not self.color:
            return super().format(record)

        # Other handlers must receive the original message and level name.
        colored = copy.copy(record)
        level_color = (
            _RED if record.levelno >= logging.ERROR else
            _YELLOW if record.levelno >= logging.WARNING else
            _BLUE if record.levelno >= logging.INFO else _GRAY
        )
        colored.levelname = f"{level_color}{record.levelname}{_RESET}"
        colored.name = f"{_GRAY}{record.name}{_RESET}"
        colored.msg = f"{_TEXT}{_message_color(record.getMessage(), record.levelno)}{_RESET}"
        colored.args = ()
        return super().format(colored)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        timestamp = super().formatTime(record, datefmt)
        return f"{_GRAY}{timestamp}{_RESET}" if self.color else timestamp


def configure_logging(level: int) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(CatppuccinFormatter(color=colors_enabled(handler.stream)))
    logging.basicConfig(level=level, handlers=[handler])
