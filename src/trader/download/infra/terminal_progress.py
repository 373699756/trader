"""Single-write terminal projection; redirected download logs stay plain text."""

from __future__ import annotations

import os
import sys
from threading import Lock
from typing import TextIO

from trader.download.application.progress_message import CleanProgressText, ProgressLevel, ProgressMessage

_COLORS = {ProgressLevel.SUCCESS: "32", ProgressLevel.WARNING: "33", ProgressLevel.ERROR: "31"}


def RenderProgress(message: ProgressMessage, *, color: bool = False) -> str:
    parts = []
    for field in message.fields:
        value = CleanProgressText(field.text)
        if color and field.level in _COLORS:
            content = value.rstrip(" ")
            value = f"\x1b[{_COLORS[field.level]}m{content}\x1b[0m" + value[len(content) :]
        parts.append(value)
    return " | ".join(parts).rstrip(" ")


class TerminalDownloadProgress:
    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._lock = Lock()
        self._color = self._stream.isatty() and os.environ.get("TERM") != "dumb" and not os.environ.get("NO_COLOR")

    def __call__(self, message: ProgressMessage) -> None:
        line = RenderProgress(message, color=self._color)
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()


__all__ = ["TerminalDownloadProgress", "RenderProgress"]
