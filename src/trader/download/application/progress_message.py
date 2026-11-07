"""Immutable, terminal-independent download feedback and display-cell layout."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Literal

from trader.infra import progress_format

ProgressState = Literal["开始", "处理中", "等待", "重试", "完成", "失败", "取消"]


@dataclass(frozen=True)
class ProgressTask:
    owner: str
    key: str
    label: str = field(compare=False)
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
    immediate: bool = False
    compact: bool = False


def FormatProgressField(text: str, width: int = 0, *, level: ProgressLevel = ProgressLevel.NORMAL) -> ProgressField:
    value = progress_format.CleanProgressText(text)
    return ProgressField(value + " " * max(0, width - progress_format.DisplayWidth(value)), level)


def ProgressNumber(value: int | None, width: int = 6) -> str:
    return ("-" if value is None else str(value)).rjust(width)


def WithProgressTime(message: ProgressMessage, total_seconds: float, stage_seconds: float) -> ProgressMessage:
    if message.task is None:
        raise ValueError("sync_progress_task_missing")
    prefix = progress_format.ProgressPrefix(
            total_seconds, f"{message.task.owner}·{message.task.label}", stage_seconds, message.state,
            stage_width=(30 if message.task.owner == "history" else 25) if message.compact else 52,
        )
    return replace(message, fields=(
        *(ProgressField(part) for part in (prefix[:2] if message.compact else prefix)),
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
    "FormatProgressField",
    "ProgressNumber",
]
