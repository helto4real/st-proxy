"""Streaming terminal color filter for Strata child-process output."""
from __future__ import annotations

import sys

from .logging import color_strata_line, colors_enabled


def main() -> None:
    color = colors_enabled(sys.stdout)
    for line in sys.stdin.buffer:
        if color:
            line = color_strata_line(line.decode("utf-8", "surrogateescape")).encode(
                "utf-8", "surrogateescape",
            )
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
