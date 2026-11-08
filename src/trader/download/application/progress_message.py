"""Immutable download feedback facts and task identities."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

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
class ProgressRow:
    label: str
    fields: tuple[ProgressField, ...]

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise ValueError("progress_row_label_missing")


@dataclass(frozen=True)
class ProgressMessage:
    fields: tuple[ProgressField, ...]
    stock_failure_code: str | None = None
    task: ProgressTask | None = None
    state: ProgressState = "处理中"
    finished: bool = False
    immediate: bool = False
    compact: bool = False
    owner_finished: bool = False
    heartbeat: bool = True
    rows: tuple[ProgressRow, ...] = ()
    heartbeat_interval_seconds: float = 5.0

    def __post_init__(self) -> None:
        if (
            type(self.heartbeat_interval_seconds) not in (int, float)
            or not math.isfinite(self.heartbeat_interval_seconds)
            or self.heartbeat_interval_seconds <= 0
        ):
            raise ValueError("sync_progress_heartbeat_interval_invalid")


__all__ = [
    "ProgressTask",
    "ProgressState",
    "ProgressField",
    "ProgressLevel",
    "ProgressMessage",
    "ProgressRow",
    "HighestProgressLevel",
]
