"""Compact human-readable stderr projection for interactive history synchronization."""

from __future__ import annotations

import time
from collections.abc import Callable
from threading import Lock

from trader.download.application.progress_message import (
    FormatProgressField,
    HighestProgressLevel,
    ProgressLevel,
    ProgressMessage,
    ProgressNumber,
)
from trader.download.application.stock_download_progress import (
    FormatStockDownloadProgress,
    StockDownloadProgress,
)
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.history_supplier_failure import HISTORY_SUPPLIER_PENDING_REASONS
from trader.download.domain.history_sync import HistorySyncProgress, HistorySyncProgressStage
from trader.download.infra.terminal_progress import TerminalDownloadProgress

_STAGE_LABELS: dict[HistorySyncProgressStage, str] = {
    "initializing": "初始化",
    "storage_profile": "历史磁盘",
    "loading_context": "加载上下文",
    "supplier_login": "供应商登录",
    "supplier_calendar": "交易日历",
    "supplier_universe": "股票总体",
    "supplier_industry": "行业快照",
    "supplier_daily_raw": "未复权日线",
    "supplier_daily_qfq": "前复权日线",
    "supplier_routing": "下载来源",
    "tencent_history": "Tencent下载",
    "history_st_reference": "历史ST名单",
    "history_active_gaps": "活动历史缺口",
    "history_gap_resume": "旧断点恢复",
    "history_gap_inventory": "统计历史缺口",
    "persisting_partitions": "正式月库提交",
    "preparing_partitions": "准备月分片",
    "downloading_codes": "股票下载",
    "sealing_partitions": "发布内容校验",
    "publishing_snapshot": "发布快照",
    "confirming_download": "下载断点提交",
    "synchronization": "同步流程",
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
    "storage_profile": "类型",
    "supplier_calendar": "日期范围",
    "supplier_industry": "日期",
    "supplier_daily_raw": "股票",
    "supplier_daily_qfq": "股票",
    "downloading_codes": "股票",
    "tencent_history": "股票",
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
        "persisting_partitions",
        "sealing_partitions",
        "supplier_industry",
    }
)
_DAILY_STAGES = frozenset({"supplier_daily_raw", "supplier_daily_qfq"})
_PREPARATION_STAGES = frozenset({"supplier_login", "supplier_calendar", "supplier_industry"})


def _suppress_transient(progress: HistorySyncProgress) -> bool:
    return progress.state in {"started", "waiting"} and not (
        progress.stage in {"history_active_gaps", "history_gap_resume", "sealing_partitions", "preparing_partitions"}
        or progress.stage in {"tencent_history", "persisting_prices"}
        and progress.state == "waiting"
    )


def _reuse_summary_parts(progress: HistorySyncProgress) -> tuple[str, ...]:
    if (
        progress.stage == "tencent_history"
        and progress.current_item is not None
        and progress.supplier_source is None
        and progress.state != "failed"
    ):
        return ("无新增或缺口，复用",)
    return ()


def _summary_parts(progress: HistorySyncProgress) -> tuple[str, ...]:
    if progress.pipeline is not None:
        pipeline = progress.pipeline
        return (
            f"网络 {ProgressNumber(pipeline.downloaded_codes)}/{ProgressNumber(progress.total_units)}",
            f"落库 {ProgressNumber(progress.completed_units)}/{ProgressNumber(progress.total_units)}",
            f"待写 {ProgressNumber(pipeline.queued_codes)}",
            f"在途 {ProgressNumber(pipeline.inflight_codes)}",
            f"待派发 {ProgressNumber(pipeline.pending_codes)}",
            f"最久在途 {_format_duration(pipeline.oldest_inflight_seconds)}",
            f"下载 {_format_duration(pipeline.download_seconds)}",
            f"恢复读库累计 {_format_duration(pipeline.read_seconds)}",
            f"校验累计 {_format_duration(pipeline.validation_seconds)}",
            f"落库耗时 {_format_duration(pipeline.write_seconds)}",
            f"本批待确认 {_format_duration(pipeline.unconfirmed_write_seconds)}",
            f"回压 {_format_duration(pipeline.backpressure_seconds)}",
        )
    return _download_summary_parts(progress)


def _download_summary_parts(progress: HistorySyncProgress) -> tuple[str, ...]:
    if progress.required_free_bytes is not None:
        return (f"保守可用空间需求 {progress.required_free_bytes / 1024**3:.2f} GiB",)
    if progress.cache_hit is not None:
        parts: tuple[str, ...] = ("缓存复用" if progress.cache_hit else "供应商查询",)
        if progress.industry_missing_codes is not None:
            parts += (f"缺行业 {ProgressNumber(progress.industry_missing_codes)} 股",)
        return parts
    if progress.tencent_summary is not None:
        summary = progress.tencent_summary
        return (
            f"完整 {ProgressNumber(summary.succeeded_codes)} 股",
            f"部分 {ProgressNumber(summary.partial_codes)} 股",
            f"失败 {ProgressNumber(summary.failed_codes)} 股",
            f"其中待重试 {ProgressNumber(summary.pending_codes)} 股",
            f"其中本地复用 {ProgressNumber(summary.reused_codes)} 股",
            f"重叠冲突 {ProgressNumber(summary.overlap_conflicts)} 股",
            f"其它失败 {ProgressNumber(summary.other_failures)} 股",
        )
    if progress.gap_summary is not None:
        gap_summary = progress.gap_summary
        return (
            f"价格对待补 {ProgressNumber(gap_summary.price_pair_cells)} 股日",
            f"本轮Tencent失败 {ProgressNumber(gap_summary.failed_codes)} 股",
        )
    if progress.st_summary is not None:
        st_summary = progress.st_summary
        return (
            f"合格 {ProgressNumber(st_summary.clear)}",
            f"曾ST排除 {ProgressNumber(st_summary.ever_st)}",
            f"未知排除 {ProgressNumber(st_summary.unknown)}",
        )
    return _reuse_summary_parts(progress)


def _state_label(progress: HistorySyncProgress) -> str:
    if progress.failure_reason in HISTORY_SUPPLIER_PENDING_REASONS:
        return "待重试（Tencent价格待补）"
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
        f"raw {ProgressNumber(coverage.raw)}/{ProgressNumber(coverage.expected)}",
        f"qfq {ProgressNumber(coverage.qfq)}/{ProgressNumber(coverage.expected)}",
        f"配对 {ProgressNumber(coverage.paired)}/{ProgressNumber(coverage.expected)}",
    )


class StderrHistorySyncProgress:
    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._lock = Lock()
        self._output = TerminalDownloadProgress()
        self._observed_level = ProgressLevel.NORMAL
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
        if progress.stage in {"downloading_codes", "tencent_history"}:
            self._stock_counts = (progress.completed_units, progress.total_units)
        self._last_progress = (progress.stage, progress.current_item)
        self._record_failure(progress)
        if self._should_suppress(progress, now):
            return
        if (
            progress.stage == "tencent_history"
            and progress.current_item is not None
            and progress.failure_detail is None
        ):
            message = self._stock_download_line(progress, now)
            self._observed_level = HighestProgressLevel(
                self._observed_level, *(field.level for field in message.fields)
            )
            self._output(message)
            return
        parts = self._progress_parts(progress, now)
        if not progress.stage.startswith("supplier_"):
            if progress.stage == "tencent_history":
                label = "总计"
            else:
                label = "本月耗时" if monthly else "阶段耗时"
            parts.append(f"{label} {_format_duration(now - self._phase_started[phase_key])}")
        level = _progress_level(progress)
        self._observed_level = HighestProgressLevel(self._observed_level, level)
        self._output(
            ProgressMessage(
                tuple(
                    FormatProgressField(
                        part,
                        14 if index == 1 else 0,
                        level=level
                        if index == 2
                        or (progress.failure_reason is not None and part == f"错误 {progress.failure_reason}")
                        else ProgressLevel.NORMAL,
                    )
                    for index, part in enumerate(parts)
                )
            )
        )

    def _stock_download_line(self, progress: HistorySyncProgress, now: float) -> ProgressMessage:
        code = progress.current_item
        if code is None:
            raise ValueError("stock download progress requires a stock code")
        coverage = progress.price_coverage
        target = coverage.expected if coverage is not None else progress.requested_sessions
        source_download = progress.supplier_source == "tencent"
        if progress.failure_reason in HISTORY_SUPPLIER_PENDING_REASONS:
            result = "待重试"
        elif progress.failure_reason is not None:
            result = "失败"
        elif not source_download:
            result = "复用"
        elif coverage is not None and coverage.missing:
            result = "部分"
        else:
            result = "合格"
        phase_key = (progress.stage, None)
        pipeline = progress.pipeline
        extra = (
            f"网络 {ProgressNumber(pipeline.downloaded_codes if pipeline else None)}/{ProgressNumber(progress.total_units)}",
            f"落库 {ProgressNumber(progress.completed_units)}/{ProgressNumber(progress.total_units)}",
            f"待写 {ProgressNumber(pipeline.queued_codes if pipeline else None)}",
            f"下载 {_format_duration(pipeline.download_seconds) if pipeline else '--:--:--'}",
            f"恢复读库累计 {_format_duration(pipeline.read_seconds) if pipeline else '--:--:--'}",
            f"校验累计 {_format_duration(pipeline.validation_seconds) if pipeline else '--:--:--'}",
            f"落库耗时 {_format_duration(pipeline.write_seconds) if pipeline else '--:--:--'}",
            f"本批待确认 {_format_duration(pipeline.unconfirmed_write_seconds) if pipeline else '--:--:--'}",
            f"回压 {_format_duration(pipeline.backpressure_seconds) if pipeline else '--:--:--'}",
            f"总计 {_format_duration(now - self._phase_started[phase_key])}",
            *_reuse_summary_parts(progress),
        )
        return FormatStockDownloadProgress(
            max(0.0, now - self._started_at),
            StockDownloadProgress(
                command="sync history",
                completed=progress.completed_units,
                total=progress.total_units,
                code=code,
                name=progress.current_name,
                action="Tencent下载" if source_download else "本地复用",
                result=result,
                target=target,
                raw=coverage.raw if coverage is not None else None,
                qfq=coverage.qfq if coverage is not None else None,
                paired=coverage.paired if coverage is not None else None,
                missing=coverage.missing if coverage is not None else None,
                inferred_suspended=coverage.inferred_suspended if coverage is not None else 0,
                reason=progress.failure_reason or "无",
                extra=extra,
            ),
        )

    def _record_failure(self, progress: HistorySyncProgress) -> None:
        if progress.state == "completed" and (
            self._specific_failure == self._last_progress
            or progress.stage.startswith("supplier_")
            and self._specific_failure is not None
            and self._specific_failure[0].startswith("supplier_")
        ):
            self._specific_failure = None
        elif progress.state == "failed" and (
            self._specific_failure is None
            or progress.stage.startswith("supplier_")
            or progress.failure_detail is not None
        ):
            self._specific_failure = self._last_progress

    def _should_suppress(self, progress: HistorySyncProgress, now: float) -> bool:
        if progress.stage == "supplier_routing":
            return True
        if progress.stage == "persisting_partitions" and progress.state == "waiting":
            return True
        if progress.stage == "persisting_partitions":
            return self._suppress_partition_progress(progress, now)
        if progress.stage in _PREPARATION_STAGES and progress.state in {"started", "waiting"}:
            return self._suppress_preparation(progress)
        if progress.stage in _DAILY_STAGES and progress.state == "completed":
            return True
        return (
            _suppress_transient(progress)
            or progress.stage == "loading_context"
            and (progress.state == "completed" or progress.state == "failed" and self._specific_failure is not None)
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
        if progress.stage in _COUNT_STAGES and progress.failure_detail is None:
            parts.append(
                f"{ProgressNumber(progress.completed_units)}/{ProgressNumber(progress.total_units)}"
                if progress.stage == "tencent_history"
                else _format_count(progress.completed_units, progress.total_units)
            )
        if progress.current_item is not None:
            item_label = _ITEM_LABELS.get(progress.stage, "当前")
            parts.append(f"{item_label} {progress.current_item}")
        parts.extend(_price_coverage_parts(progress))
        parts.extend(_summary_parts(progress))
        if (
            progress.stage in {"downloading_codes", "tencent_history"}
            and (
                progress.supplier_source is not None
                or progress.stage == "downloading_codes"
                and self._route is not None
            )
            and progress.stage != "tencent_history"
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
        if progress.failure_detail is not None:
            parts.append(f"诊断 {progress.failure_detail}")
        return parts

    def _result_parts(self, status: HistoryMaintenanceStatus) -> list[str]:
        parts = [_format_duration(self._elapsed()), _RESULT_LABELS[status.state]]
        if status.state == "failed":
            location = self._specific_failure
            if location is not None:
                stage, current_item = location
                parts.append(_STAGE_LABELS[stage])
                if current_item is not None:
                    parts.append(f"{_ITEM_LABELS.get(stage, '当前')} {current_item}")
            else:
                parts.append("故障位置未知")
        if status.state in {"failed", "blocked"}:
            if status.reason is not None:
                parts.append(f"错误 {status.reason}")
        if status.unresolved_price_cells:
            if status.state in {"completed", "already_current"}:
                parts.append("价格缺口待补")
            parts.append(f"未修复价格缺口 {status.unresolved_price_cells} 股日")
            if status.price_gap_summary is not None:
                summary = status.price_gap_summary
                parts.append(f"涉及 {summary.stocks} 股 | 日期 {summary.first_date}～{summary.last_date}")
                parts.extend(f"{item.reason} {item.cells} 股日" for item in summary.reasons)
        return parts

    def publish_result(self, status: HistoryMaintenanceStatus) -> None:
        parts = self._result_parts(status)
        level = HighestProgressLevel(_maintenance_result_level(status), self._observed_level)
        if status.state in {"completed", "already_current"} and not status.unresolved_price_cells:
            if level is ProgressLevel.ERROR:
                parts.append("本轮有失败记录")
            elif level is ProgressLevel.WARNING:
                parts.append("本轮有不完整或待重试记录")
        self._output(
            ProgressMessage(
                tuple(
                    FormatProgressField(
                        part,
                        level=level
                        if index == 1
                        or index == len(parts) - 1
                        or part == f"错误 {status.reason}"
                        or part == "价格缺口待补"
                        or part == f"未修复价格缺口 {status.unresolved_price_cells} 股日"
                        else ProgressLevel.NORMAL,
                    )
                    for index, part in enumerate(parts)
                )
            )
        )

    def _elapsed(self) -> float:
        return max(0.0, self._monotonic() - self._started_at)


def _format_count(completed: int, total: int) -> str:
    percent = 100.0 if total == 0 else completed / total * 100.0
    return f"{ProgressNumber(completed)}/{ProgressNumber(total)} ({percent:6.2f}%)"


def _progress_level(progress: HistorySyncProgress) -> ProgressLevel:
    level = ProgressLevel.NORMAL
    if (
        progress.stage == "history_st_reference"
        and progress.completed_units < progress.total_units
        and progress.state == "completed"
    ):
        level = ProgressLevel.NORMAL
    elif progress.failure_reason in HISTORY_SUPPLIER_PENDING_REASONS or progress.state in {"retrying", "cancelled"}:
        level = ProgressLevel.WARNING
    elif progress.state == "failed":
        level = ProgressLevel.WARNING if progress.attempt < progress.max_attempts else ProgressLevel.ERROR
    elif progress.price_coverage is not None and progress.price_coverage.missing:
        level = ProgressLevel.WARNING
    elif progress.industry_missing_codes:
        level = ProgressLevel.WARNING
    elif progress.tencent_summary is not None:
        summary = progress.tencent_summary
        level = (
            ProgressLevel.ERROR
            if summary.failed_codes
            else (ProgressLevel.WARNING if summary.partial_codes or summary.pending_codes else ProgressLevel.SUCCESS)
        )
    elif progress.state == "completed":
        level = ProgressLevel.SUCCESS
    return level


def _maintenance_result_level(status: HistoryMaintenanceStatus) -> ProgressLevel:
    if status.state in {"failed", "blocked"}:
        return ProgressLevel.ERROR
    if status.state == "cancelled" or status.unresolved_price_cells:
        return ProgressLevel.WARNING
    if status.state in {"completed", "already_current"}:
        return ProgressLevel.SUCCESS
    return ProgressLevel.NORMAL


def _format_duration(seconds: float) -> str:
    total_seconds = int(max(0.0, seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"


__all__ = ["StderrHistorySyncProgress"]
