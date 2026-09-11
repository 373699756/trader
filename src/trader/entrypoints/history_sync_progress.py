"""Compact human-readable stderr projection for interactive history synchronization."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable

from trader.application.research.history_maintenance import HistoryMaintenanceStatus
from trader.application.research.history_sync import HistorySyncProgress, HistorySyncProgressStage

_STAGE_LABELS: dict[HistorySyncProgressStage, str] = {
    "initializing": "初始化",
    "loading_context": "加载上下文",
    "supplier_login": "供应商登录",
    "supplier_calendar": "交易日历",
    "supplier_universe": "股票总体",
    "supplier_industry": "行业快照",
    "supplier_daily_raw": "未复权日线",
    "supplier_daily_qfq": "前复权日线",
    "preparing_partitions": "准备月分片",
    "downloading_codes": "股票下载",
    "sealing_partitions": "封存月分片",
    "publishing_snapshot": "发布快照",
}
_STATE_LABELS = {
    "started": "开始",
    "waiting": "等待",
    "retrying": "重试",
    "completed": "完成",
    "failed": "失败",
    "cancelled": "取消",
}
_ITEM_LABELS: dict[HistorySyncProgressStage, str] = {
    "supplier_calendar": "日期范围",
    "supplier_industry": "日期",
    "supplier_daily_raw": "股票",
    "supplier_daily_qfq": "股票",
    "downloading_codes": "股票",
    "sealing_partitions": "月份",
}
_RESULT_LABELS = {
    "completed": "同步完成",
    "already_current": "已是最新",
    "already_running": "已有同步运行",
    "cancelled": "同步已取消",
    "blocked": "同步阻塞",
    "failed": "同步失败",
}
_COUNT_STAGES = frozenset({"downloading_codes", "sealing_partitions"})
_DAILY_STAGES = frozenset({"supplier_daily_raw", "supplier_daily_qfq"})


class StderrHistorySyncProgress:
    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._started_at = monotonic()
        self._stock_counts: tuple[int, int] | None = None
        self._last_progress: tuple[HistorySyncProgressStage, str | None] | None = None
        self._specific_failure: tuple[HistorySyncProgressStage, str | None] | None = None

    def publish(self, progress: HistorySyncProgress) -> None:
        if progress.stage == "downloading_codes":
            self._stock_counts = (progress.completed_units, progress.total_units)
        self._last_progress = (progress.stage, progress.current_item)
        self._record_supplier_failure(progress)
        if self._should_suppress(progress):
            return
        self._print(self._progress_parts(progress))

    def _record_supplier_failure(self, progress: HistorySyncProgress) -> None:
        if not progress.stage.startswith("supplier_"):
            return
        if progress.state == "completed":
            self._specific_failure = None
        elif progress.state == "failed":
            self._specific_failure = self._last_progress

    def _should_suppress(self, progress: HistorySyncProgress) -> bool:
        if progress.state in {"started", "waiting"}:
            return True
        if progress.stage in _DAILY_STAGES and progress.state == "completed":
            return True
        return progress.stage == "loading_context" and (
            progress.state == "completed" or progress.state == "failed" and self._specific_failure is not None
        )

    def _progress_parts(self, progress: HistorySyncProgress) -> list[str]:
        parts = [
            _format_duration(self._elapsed()),
            _STAGE_LABELS[progress.stage],
            _STATE_LABELS[progress.state],
        ]
        if progress.stage in _COUNT_STAGES:
            parts.append(_format_count(progress.completed_units, progress.total_units))
        if progress.current_item is not None:
            item_label = _ITEM_LABELS.get(progress.stage, "当前")
            parts.append(f"{item_label} {progress.current_item}")
        if progress.stage in _DAILY_STAGES and self._stock_counts is not None:
            parts.append(f"总进度 {_format_count(*self._stock_counts)}")
        if progress.max_attempts > 1:
            parts.append(f"尝试 {progress.attempt}/{progress.max_attempts}")
        if progress.call_elapsed_seconds > 0.0:
            parts.append(f"调用 {_format_duration(progress.call_elapsed_seconds)}")
        return parts

    def publish_result(self, status: HistoryMaintenanceStatus) -> None:
        parts = [_format_duration(self._elapsed()), _RESULT_LABELS[status.state]]
        if status.state == "failed":
            location = self._specific_failure or self._last_progress
            if location is not None:
                stage, current_item = location
                parts.append(_STAGE_LABELS[stage])
                if current_item is not None:
                    parts.append(f"{_ITEM_LABELS.get(stage, '当前')} {current_item}")
        if status.state in {"failed", "blocked"}:
            if status.reason is not None:
                parts.append(f"错误 {status.reason}")
        self._print(parts)

    def _elapsed(self) -> float:
        return max(0.0, self._monotonic() - self._started_at)

    @staticmethod
    def _print(parts: list[str]) -> None:
        print(" | ".join(parts), file=sys.stderr, flush=True)


def _format_count(completed: int, total: int) -> str:
    percent = 100.0 if total == 0 else completed / total * 100.0
    return f"{completed}/{total} ({percent:.2f}%)"


def _format_duration(seconds: float) -> str:
    total_seconds = int(max(0.0, seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"


__all__ = ["StderrHistorySyncProgress"]
