"""Shared human-readable projection for per-stock download outcomes."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Literal

_DownloadCommand = Literal["download", "qfq_download"]


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

    def __post_init__(self) -> None:
        counts = (self.raw, self.qfq, self.paired)
        if (
            self.command not in ("download", "qfq_download")
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
            or self.name is not None
            and not self.name.strip()
            or any(not item.strip() for item in self.extra)
        ):
            raise ValueError("stock download progress is invalid")


def format_stock_download_progress(elapsed_seconds: float, progress: StockDownloadProgress) -> str:
    if not math.isfinite(elapsed_seconds):
        raise ValueError("stock download elapsed time is invalid")
    percent = progress.completed / progress.total * 100 if progress.total else 100.0
    name = None if progress.name is None else " ".join(progress.name.replace("|", " ").split())
    stock = progress.code if not name else f"{progress.code} {name}"
    missing = "未知" if progress.missing is None else str(progress.missing)
    if progress.missing_dates:
        missing += f"（{_format_missing_dates(progress.missing_dates)}）"
    parts = [
        _format_duration(elapsed_seconds),
        f"{progress.command} 股票进度",
        f"{progress.completed}/{progress.total}（{percent:.2f}%）",
        f"股票 {stock}",
        f"处理 {progress.action}",
        f"结果 {progress.result}",
        f"raw {_format_count(progress.raw, progress.target)}",
        f"qfq {_format_count(progress.qfq, progress.target)}",
        f"完整配对 {_format_count(progress.paired, progress.target)}",
        f"缺日 {missing}",
        f"原因 {progress.reason}",
        *progress.extra,
    ]
    return " | ".join(parts)


def _format_count(value: int | None, target: int | None) -> str:
    actual = "-" if value is None else str(value)
    expected = "-" if target is None else str(target)
    return f"{actual}/{expected}"


def _format_missing_dates(days: tuple[date, ...]) -> str:
    preview = ", ".join(day.isoformat() for day in days[:8])
    if len(days) > 8:
        preview += f" 等 {len(days)} 个交易日"
    return preview


def _format_duration(seconds: float) -> str:
    total_seconds = int(max(0.0, seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"


__all__ = ["StockDownloadProgress", "format_stock_download_progress"]
