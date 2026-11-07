"""Immutable, terminal-independent download feedback and display-cell layout."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from enum import Enum
from typing import Literal, cast

ProgressState = Literal["开始", "处理中", "等待", "重试", "完成", "失败", "取消"]


@dataclass(frozen=True)
class ProgressTask:
    owner: str
    key: str
    label: str
    execution: int = 0

    def __post_init__(self) -> None:
        if any(not value.strip() for value in (self.owner, self.key, self.label)) or self.execution < 0:
            raise ValueError("sync_progress_task_invalid")


class ProgressLevel(Enum):
    NORMAL = "normal"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


def HighestProgressLevel(*levels: ProgressLevel) -> ProgressLevel:
    order = tuple(ProgressLevel)
    return max(levels, key=order.index, default=ProgressLevel.NORMAL)


@dataclass(frozen=True)
class ProgressField:
    text: str
    level: ProgressLevel = ProgressLevel.NORMAL


@dataclass(frozen=True)
class ProgressMessage:
    fields: tuple[ProgressField, ...]
    stock_failure_code: str | None = None
    task: ProgressTask | None = None
    state: ProgressState = "处理中"
    finished: bool = False


def CleanProgressText(text: str) -> str:
    # Remove complete escape sequences, not only ESC (which leaves fake colour text).
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(" " if char == "|" or unicodedata.category(char).startswith("C") else char for char in text)


def DisplayWidth(text: str) -> int:
    return sum(
        0
        if unicodedata.category(char) in {"Mn", "Me"}
        else 2
        if unicodedata.east_asian_width(char) in {"W", "F"}
        else 1
        for char in text
    )


def FormatProgressField(text: str, width: int = 0, *, level: ProgressLevel = ProgressLevel.NORMAL) -> ProgressField:
    value = CleanProgressText(text)
    return ProgressField(value + " " * max(0, width - DisplayWidth(value)), level)


def ProgressNumber(value: int | None, width: int = 6) -> str:
    return ("-" if value is None else str(value)).rjust(width)


def ProgressDuration(seconds: float) -> str:
    hours, remainder = divmod(int(max(0, seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def WithProgressTime(message: ProgressMessage, total_seconds: float, stage_seconds: float) -> ProgressMessage:
    if message.task is None:
        raise ValueError("sync_progress_task_missing")
    return replace(message, fields=(
        ProgressField(ProgressDuration(total_seconds)),
        FormatProgressField(f"{message.task.owner}·{message.task.label} {ProgressDuration(stage_seconds)}", 36),
        FormatProgressField(message.state, 6),
        *message.fields,
    ))


__all__ = [
    "ProgressTask",
    "ProgressState",
    "WithProgressTime",
    "ProgressField",
    "ProgressLevel",
    "ProgressMessage",
    "HighestProgressLevel",
    "CleanProgressText",
    "DisplayWidth",
    "FormatProgressField",
    "ProgressNumber",
    "ProgressDuration",
]


def _startup_progress() -> None:
    """Dependency-free shell feedback before the package has been installed."""
    import os
    import sys
    import time
    from threading import Event, Lock, Thread

    started = float(os.environ["TRADER_SYNC_STARTED_MONOTONIC"])
    lock = Lock()

    def emit(state: ProgressState, text: str) -> None:
        elapsed = time.monotonic() - started
        message = WithProgressTime(ProgressMessage(
            (ProgressField(text),), task=ProgressTask("启动", "environment", "运行环境检查"), state=state,
        ), elapsed, elapsed)
        with lock:
            print(" | ".join(CleanProgressText(field.text) for field in message.fields), file=sys.stderr, flush=True)

    if sys.argv[1] != "--pipe":
        if sys.argv[1] not in {"开始", "处理中", "等待", "重试", "完成", "失败", "取消"}:
            raise ValueError("sync_progress_state_invalid")
        emit(cast(ProgressState, sys.argv[1]), sys.argv[2])
        return
    stopped = Event()

    def heartbeat() -> None:
        while not stopped.wait(5):
            emit("等待", "环境依赖准备仍在进行")

    thread = Thread(target=heartbeat, name="sync-environment-progress", daemon=False)
    thread.start()
    try:
        for line in sys.stdin:
            emit("处理中", line.rstrip())
    finally:
        stopped.set()
        thread.join()


if __name__ == "__main__":
    _startup_progress()
