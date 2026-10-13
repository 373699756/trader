"""Shared human-readable projection for per-stock download outcomes."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.download.application.progress_message import (
    ProgressField,
    ProgressLevel,
    ProgressMessage,
    clean_progress_text,
    display_width,
    progress_duration,
    progress_field,
    progress_number,
)

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
    failure_level: ProgressLevel = ProgressLevel.WARNING

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


def format_stock_download_progress(elapsed_seconds: float, progress: StockDownloadProgress) -> ProgressMessage:
    if not math.isfinite(elapsed_seconds):
        raise ValueError("stock download elapsed time is invalid")
    percent = progress.completed / progress.total * 100 if progress.total else 100.0
    name = clean_progress_text(progress.name or "—").strip() or "—"
    short_name = name
    if display_width(name) > 16:
        short_name = ""
        for char in name:
            if display_width(short_name + char) > 15:
                break
            short_name += char
        short_name += "…"
    level = _result_level(progress)
    missing = "未知".rjust(4) if progress.missing is None else progress_number(progress.missing)
    parts = [
        progress_field(progress_duration(elapsed_seconds), 8),
        progress_field(f"{progress.command} 股票进度", 21),
        ProgressField(f"{progress_number(progress.completed)}/{progress_number(progress.total)}（{percent:6.2f}%）"),
        progress_field(f"股票 {progress.code} {short_name}", 28),
        progress_field(f"处理 {progress.action}", 16),
        progress_field(f"结果 {progress.result}", 13, level=level),
        progress_field(f"raw {_format_count(progress.raw, progress.target)}"),
        progress_field(f"qfq {_format_count(progress.qfq, progress.target)}"),
        progress_field(f"完整配对 {_format_count(progress.paired, progress.target)}"),
        progress_field(
            f"缺日 {missing}", 11, level=ProgressLevel.WARNING if progress.missing != 0 else ProgressLevel.NORMAL
        ),
        *(progress_field(item) for item in progress.extra),
        progress_field(f"原因 {progress.reason}", level=level if progress.reason != "无" else ProgressLevel.NORMAL),
    ]
    if progress.missing_dates:
        parts.append(progress_field(f"缺失日期 {_format_missing_dates(progress.missing_dates)}"))
    if short_name != name:
        parts.append(progress_field(f"完整名称 {name}"))
    return ProgressMessage(tuple(parts), progress.reason if progress.result in {"失败", "待重试"} else None)


def _result_level(progress: StockDownloadProgress) -> ProgressLevel:
    if progress.result == "失败":
        return ProgressLevel.ERROR
    if progress.result == "待重试":
        return progress.failure_level
    if progress.result == "部分" or progress.missing != 0:
        return ProgressLevel.WARNING
    if progress.result == "合格":
        return ProgressLevel.SUCCESS
    return ProgressLevel.NORMAL


def _format_count(value: int | None, target: int | None) -> str:
    return f"{progress_number(value)}/{progress_number(target)}"


def _format_missing_dates(days: tuple[date, ...]) -> str:
    preview = ", ".join(day.isoformat() for day in days[:8])
    if len(days) > 8:
        preview += f" 等 {len(days)} 个交易日"
    return preview


__all__ = ["StockDownloadProgress", "format_stock_download_progress"]
