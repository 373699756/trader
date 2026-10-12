"""Compact human-readable stderr projection for interactive history synchronization."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from threading import Lock

from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.history_sync import HistorySyncProgress, HistorySyncProgressStage

_STAGE_LABELS: dict[HistorySyncProgressStage, str] = {
    "initializing": "初始化",
    "loading_context": "加载上下文",
    "supplier_login": "供应商登录",
    "supplier_calendar": "交易日历",
    "supplier_universe": "股票总体",
    "supplier_industry": "行业快照",
    "supplier_daily_raw": "未复权日线",
    "supplier_daily_qfq": "前复权日线",
    "supplier_routing": "下载来源",
    "tencent_history": "Tencent历史下载",
    "history_st_reference": "历史ST名单",
    "history_active_gaps": "活动历史缺口",
    "history_gap_resume": "旧断点恢复",
    "history_gap_inventory": "统计历史缺口",
    "baostock_gap_fill": "BaoStock补缺",
    "persisting_partitions": "正式月库提交",
    "preparing_partitions": "准备月分片",
    "downloading_codes": "股票下载",
    "sealing_partitions": "发布内容校验",
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
    "tencent_history": "股票",
    "baostock_gap_fill": "股票",
    "persisting_partitions": "月份",
    "sealing_partitions": "月份",
    "history_active_gaps": "月份",
    "history_gap_resume": "处理项",
}
_RESULT_LABELS = {
    "completed": "同步完成",
    "already_current": "已是最新",
    "already_running": "已有同步运行",
    "cancelled": "同步已取消",
    "blocked": "同步阻塞",
    "failed": "同步失败",
}
_COUNT_STAGES = frozenset(
    {
        "history_st_reference",
        "history_active_gaps",
        "history_gap_resume",
        "downloading_codes",
        "tencent_history",
        "baostock_gap_fill",
        "persisting_partitions",
        "sealing_partitions",
        "supplier_industry",
    }
)
_DAILY_STAGES = frozenset({"supplier_daily_raw", "supplier_daily_qfq"})
_PREPARATION_STAGES = frozenset({"supplier_login", "supplier_calendar", "supplier_industry"})


def _recovery_summary_parts(progress: HistorySyncProgress) -> tuple[str, ...]:
    if progress.stage == "tencent_history" and progress.current_item is not None and progress.supplier_source is None:
        return ("无新增或缺口，复用",)
    if progress.recovery_summary is not None:
        recovery = progress.recovery_summary
        return (
            f"缺口股 {recovery.total}",
            f"成功 {recovery.succeeded_codes} 股",
            f"补缺失败 {recovery.failed_codes} 股",
            f"未处理完 {recovery.unattempted_codes} 股",
        )
    return ()


def _summary_parts(progress: HistorySyncProgress) -> tuple[str, ...]:
    if progress.pipeline is not None:
        pipeline = progress.pipeline
        return (
            f"网络已返回/复用 {pipeline.downloaded_codes} 股",
            f"已持久化 {progress.completed_units} 股",
            f"待写库 {pipeline.queued_codes} 股",
            f"下载累计 {pipeline.download_seconds:.2f} 秒",
            f"写库累计 {pipeline.write_seconds:.2f} 秒",
            f"回压等待 {pipeline.backpressure_seconds:.2f} 秒",
        )
    return _download_summary_parts(progress)


def _download_summary_parts(progress: HistorySyncProgress) -> tuple[str, ...]:
    if progress.required_free_bytes is not None:
        return (f"保守可用空间需求 {progress.required_free_bytes / 1024**3:.2f} GiB",)
    if progress.cache_hit is not None:
        return ("缓存复用" if progress.cache_hit else "供应商查询",)
    if progress.tencent_summary is not None:
        summary = progress.tencent_summary
        return (
            f"完整 {summary.succeeded_codes} 股",
            f"部分 {summary.partial_codes} 股",
            f"失败 {summary.failed_codes} 股",
            f"其中待重试 {summary.pending_codes} 股",
            f"其中本地复用 {summary.reused_codes} 股",
            f"重叠冲突 {summary.overlap_conflicts} 股",
            f"其它失败 {summary.other_failures} 股",
        )
    if progress.gap_summary is not None:
        gap_summary = progress.gap_summary
        return (
            f"价格对待补 {gap_summary.price_pair_cells} 股日",
            f"本轮Tencent失败 {gap_summary.failed_codes} 股",
        )
    if progress.st_summary is not None:
        st_summary = progress.st_summary
        return (
            f"合格 {st_summary.clear}",
            f"曾ST排除 {st_summary.ever_st}",
            f"未知排除 {st_summary.unknown}",
        )
    return _recovery_summary_parts(progress)


def _state_label(progress: HistorySyncProgress) -> str:
    if progress.failure_reason == "tencent_history_pending":
        return "待重试（Tencent临时失败）"
    if progress.price_coverage is not None and progress.price_coverage.missing and progress.state == "completed":
        return "完成（按Tencent实际返回）"
    if progress.price_coverage is not None and not progress.price_coverage.missing and progress.state == "completed":
        return "覆盖完成（已持久化）"
    if (
        progress.stage == "history_st_reference"
        and progress.state == "completed"
        and progress.completed_units < progress.total_units
    ):
        return "检查中"
    return _STATE_LABELS[progress.state]


def _price_coverage_parts(progress: HistorySyncProgress) -> tuple[str, ...]:
    coverage = progress.price_coverage
    if coverage is None:
        return ()
    return (
        f"本次应有 {coverage.expected} 日",
        f"raw {coverage.raw} 日",
        f"qfq {coverage.qfq} 日",
        f"完整配对 {coverage.paired} 日",
        f"Tencent未返回 {coverage.missing} 日",
        f"窗口交易日 {coverage.calendar_sessions if coverage.calendar_sessions is not None else coverage.expected} 日",
        f"上市前/退市后不适用 {coverage.not_applicable} 日",
    )


class StderrHistorySyncProgress:
    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._lock = Lock()
        self._monotonic = monotonic
        self._started_at = monotonic()
        self._stock_counts: tuple[int, int] | None = None
        self._last_progress: tuple[HistorySyncProgressStage, str | None] | None = None
        self._specific_failure: tuple[HistorySyncProgressStage, str | None] | None = None
        self._route: HistorySyncProgress | None = None
        self._preparation_started: tuple[HistorySyncProgressStage, str | None, int] | None = None
        self._partition_reported_at: float | None = None
        self._phase_started: dict[tuple[HistorySyncProgressStage, str | None], float] = {}

    def publish(self, progress: HistorySyncProgress) -> None:
        with self._lock:
            self._publish(progress)

    def _publish(self, progress: HistorySyncProgress) -> None:
        now = self._monotonic()
        monthly = progress.stage in {"persisting_partitions", "sealing_partitions", "history_active_gaps"}
        phase_key = (progress.stage, progress.current_item if monthly else None)
        self._phase_started.setdefault(phase_key, now)
        if progress.stage == "supplier_routing":
            self._route = progress
        if progress.stage in {"downloading_codes", "tencent_history", "baostock_gap_fill"}:
            self._stock_counts = (progress.completed_units, progress.total_units)
        self._last_progress = (progress.stage, progress.current_item)
        self._record_supplier_failure(progress)
        if self._should_suppress(progress, now):
            return
        parts = self._progress_parts(progress, now)
        if not progress.stage.startswith("supplier_"):
            label = "本月耗时" if monthly else "阶段耗时"
            parts.append(f"{label} {_format_duration(now - self._phase_started[phase_key])}")
        self._print(parts)

    def _record_supplier_failure(self, progress: HistorySyncProgress) -> None:
        if not progress.stage.startswith("supplier_"):
            return
        if progress.state == "completed":
            self._specific_failure = None
        elif progress.state == "failed":
            self._specific_failure = self._last_progress

    def _should_suppress(self, progress: HistorySyncProgress, now: float) -> bool:
        if progress.stage == "supplier_routing":
            return True
        if progress.stage == "persisting_partitions":
            return self._suppress_partition_progress(progress, now)
        if progress.stage in _PREPARATION_STAGES and progress.state in {"started", "waiting"}:
            return self._suppress_preparation(progress)
        if progress.state in {"started", "waiting"} and not (
            progress.stage
            in {
                "history_active_gaps",
                "history_gap_resume",
                "sealing_partitions",
                "preparing_partitions",
                "baostock_gap_fill",
            }
            or progress.stage in {"tencent_history", "persisting_prices"}
            and progress.state == "waiting"
        ):
            return True
        if progress.stage in _DAILY_STAGES and progress.state == "completed":
            return True
        return progress.stage == "loading_context" and (
            progress.state == "completed" or progress.state == "failed" and self._specific_failure is not None
        )

    def _suppress_partition_progress(self, progress: HistorySyncProgress, now: float) -> bool:
        if progress.state == "completed" and progress.completed_units == progress.total_units:
            self._partition_reported_at = None
            return False
        if self._partition_reported_at is None or now - self._partition_reported_at >= 5.0:
            self._partition_reported_at = now
            return False
        return True

    def _suppress_preparation(self, progress: HistorySyncProgress) -> bool:
        if progress.state == "waiting":
            return progress.call_elapsed_seconds < 5.0
        key = (progress.stage, progress.current_item, progress.attempt)
        duplicate = key == self._preparation_started
        self._preparation_started = key
        return duplicate

    def _progress_parts(self, progress: HistorySyncProgress, now: float) -> list[str]:
        parts = [
            _format_duration(max(0.0, now - self._started_at)),
            _STAGE_LABELS[progress.stage],
            _state_label(progress),
        ]
        if progress.stage in _COUNT_STAGES:
            parts.append(_format_count(progress.completed_units, progress.total_units))
        parts.extend(_summary_parts(progress))
        parts.extend(_price_coverage_parts(progress))
        if progress.current_item is not None:
            item_label = _ITEM_LABELS.get(progress.stage, "当前")
            parts.append(f"{item_label} {progress.current_item}")
        if progress.stage in {"downloading_codes", "tencent_history", "baostock_gap_fill"} and (
            progress.supplier_source is not None or progress.stage == "downloading_codes" and self._route is not None
        ):
            if progress.stage == "downloading_codes" and self._route is not None:
                source = "Tencent" if self._route.supplier_source == "tencent" else "BaoStock"
                requested = self._route.requested_sessions
            else:
                source = "Tencent" if progress.supplier_source == "tencent" else "BaoStock"
                requested = progress.requested_sessions
            parts.extend((f"来源 {source}", f"请求 {requested} 日"))
        if progress.stage in _DAILY_STAGES and self._stock_counts is not None:
            parts.append(f"总进度 {_format_count(*self._stock_counts)}")
        if progress.max_attempts > 1:
            parts.append(f"尝试 {progress.attempt}/{progress.max_attempts}")
        if progress.call_elapsed_seconds > 0.0:
            parts.append(f"调用 {_format_duration(progress.call_elapsed_seconds)}")
        if progress.failure_reason is not None:
            parts.append(f"错误 {progress.failure_reason}")
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
        if status.unresolved_price_cells:
            if status.state == "completed":
                parts.append("数据不完整")
            parts.append(f"未修复价格缺口 {status.unresolved_price_cells} 股日")
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
