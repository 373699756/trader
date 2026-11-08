"""Immutable per-stock download facts shared with output adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.download.application.progress_message import ProgressLevel

_DownloadCommand = Literal["sync history", "sync market"]


@dataclass(frozen=True)
class StockDownloadProgress:
    command: _DownloadCommand
    completed: int
    total: int
    code: str
    name: str | None
    action: str
    result: str
    target: int | None
    raw: int | None
    qfq: int | None
    paired: int | None
    missing: int | None
    missing_dates: tuple[date, ...] = ()
    reason: str = "无"
    extra: tuple[str, ...] = ()
    failure_level: ProgressLevel = ProgressLevel.WARNING
    inferred_suspended: int = 0
    source: str | None = None

    def __post_init__(self) -> None:
        counts = (self.raw, self.qfq, self.paired)
        if (
            self.command not in ("sync history", "sync market")
            or type(self.completed) is not int
            or type(self.total) is not int
            or not 0 <= self.completed <= self.total
            or not self.code.strip()
            or not self.action.strip()
            or not self.result.strip()
            or not self.reason.strip()
            or self.target is not None
            and (type(self.target) is not int or self.target < 0)
            or any(value is not None and (type(value) is not int or value < 0) for value in counts)
            or self.target is not None
            and any(value is not None and value > self.target for value in counts)
            or self.missing is not None
            and (type(self.missing) is not int or self.missing < 0)
            or type(self.inferred_suspended) is not int
            or self.inferred_suspended < 0
            or self.name is not None
            and not self.name.strip()
            or any(not item.strip() for item in self.extra)
        ):
            raise ValueError("stock download progress is invalid")


__all__ = ["StockDownloadProgress"]
