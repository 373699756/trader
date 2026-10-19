"""Chinese batch feedback for paired-window updates, using one accounting owner."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from trader.download.application.progress_message import (
    FormatProgressField,
    ProgressDuration,
    ProgressField,
    ProgressLevel,
    ProgressMessage,
    ProgressNumber,
)
from trader.download.application.stock_download_progress import (
    FormatStockDownloadProgress,
    StockDownloadProgress,
)
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_supplier_failure import HistorySupplierPendingError
from trader.download.domain.history_sync import HistoryStEligibilitySummary
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.domain.qfq_window import QfqPendingReason, QfqUpdateResult, QfqWindowIncompleteError


def QfqMessage(
    elapsed: float, stage: str, details: str, *, level: ProgressLevel = ProgressLevel.NORMAL
) -> ProgressMessage:
    return ProgressMessage(
        (
            FormatProgressField(ProgressDuration(elapsed), 8),
            FormatProgressField(f"qfq {stage}", 24, level=level),
            *(
                FormatProgressField(part, level=level if level is ProgressLevel.ERROR else ProgressLevel.NORMAL)
                for part in details.split(" | ")
            ),
        )
    )


def ReportQfqStProgress(
    report: Callable[[ProgressMessage], None],
    elapsed: float,
    completed: int,
    total: int,
    summary: HistoryStEligibilitySummary | None,
) -> None:
    if summary is None and completed % 256 != 0:
        return
    details = f"{ProgressNumber(completed)}/{ProgressNumber(total)}"
    if summary is not None:
        details += f" | 合格 {ProgressNumber(summary.clear)} | 曾ST排除 {ProgressNumber(summary.ever_st)} | 未知排除 {ProgressNumber(summary.unknown)}"
    report(QfqMessage(elapsed, "ST资格", details))


def QfqFailureCode(exc: Exception) -> str:
    message = str(exc)
    return message if re.fullmatch(r"[a-zA-Z0-9_]{1,64}", message) else type(exc).__name__


def _window_counts(window: PublishedHistoryWindow) -> tuple[int, int, int]:
    raw = sum(cell.unadjusted is not None for cell in window.cells)
    qfq = sum(cell.qfq is not None for cell in window.cells)
    paired = sum(cell.unadjusted is not None and cell.qfq is not None for cell in window.cells)
    return raw, qfq, paired


@dataclass(frozen=True)
class QfqStockFeedback:
    action: str
    result: str
    target: int
    raw: int | None
    qfq: int | None
    paired: int | None
    missing_dates: tuple[date, ...] = ()
    inferred_suspended: int = 0
    reason: ProgressField = ProgressField("无")


@dataclass
class QfqDownloadProgress:
    day: date
    source: str
    total: int
    report: Callable[[ProgressMessage], None]
    elapsed: Callable[[], float]
    completed: int = 0
    pending: int = 0
    skipped: int = 0
    not_applicable: int = 0
    no_trade_data: int = 0
    latest_missing: int = 0
    rows: int = 0
    errors: int = 0
    write_seconds: float = 0.0
    changed: set[str] = field(default_factory=set)
    pending_reasons: tuple[QfqPendingReason, ...] = ()

    @property
    def processed(self) -> int:
        return self.completed + self.pending + self.not_applicable + self.no_trade_data

    def publish(self, stage: str, details: str, *, level: ProgressLevel = ProgressLevel.NORMAL) -> None:
        self.report(QfqMessage(self.elapsed(), stage, details, level=level))

    def record_stock(self, security: BaoStockSecurity, feedback: QfqStockFeedback) -> None:
        self.report(
            FormatStockDownloadProgress(
                self.elapsed(),
                StockDownloadProgress(
                    command="sync market",
                    completed=self.processed,
                    total=self.total,
                    code=security.code,
                    name=security.name,
                    action=feedback.action,
                    result=feedback.result,
                    target=feedback.target,
                    raw=feedback.raw,
                    qfq=feedback.qfq,
                    paired=feedback.paired,
                    missing=feedback.target - feedback.paired if feedback.paired is not None else None,
                    missing_dates=feedback.missing_dates,
                    inferred_suspended=feedback.inferred_suspended,
                    reason=feedback.reason.text,
                    failure_level=ProgressLevel.ERROR
                    if feedback.reason.level is ProgressLevel.ERROR
                    else ProgressLevel.WARNING,
                ),
            )
        )

    def record_pending(
        self,
        security: BaoStockSecurity,
        exc: Exception,
        dates: tuple[date, ...],
        *,
        action: str,
        failure_level: ProgressLevel = ProgressLevel.WARNING,
    ) -> None:
        self.pending += 1
        reason = "qfq_window_incomplete" if isinstance(exc, QfqWindowIncompleteError) else QfqFailureCode(exc)
        self._add_pending_reason(reason)
        if action == "本地检查" or failure_level is ProgressLevel.ERROR:
            self.errors += 1
        target = len(dates)
        raw: int | None = None
        qfq: int | None = None
        paired: int | None = None
        missing_dates: tuple[date, ...] = ()
        if isinstance(exc, QfqWindowIncompleteError):
            raw = target - len(exc.raw_missing)
            qfq = target - len(exc.qfq_missing)
            missing_dates = tuple(sorted(set(exc.raw_missing) | set(exc.qfq_missing)))
            paired = target - len(missing_dates)
        elif isinstance(exc, HistorySupplierPendingError):
            raw, qfq, paired = _window_counts(exc.window)
            cells = {cell.trade_date: cell for cell in exc.window.cells}
            missing_dates = tuple(
                day for day in dates if day not in cells or cells[day].unadjusted is None or cells[day].qfq is None
            )
        self.record_stock(
            security,
            QfqStockFeedback(
                action,
                "待重试" if action == "Tencent下载" else "失败",
                target,
                raw,
                qfq,
                paired,
                missing_dates=missing_dates,
                reason=ProgressField(QfqFailureCode(exc), failure_level),
            ),
        )

    def record_no_trade_data(self, security: BaoStockSecurity, window: PublishedHistoryWindow) -> None:
        self.no_trade_data += 1
        self.record_stock(
            security,
            QfqStockFeedback("Tencent下载", "无真实日线", 0, 0, 0, 0, inferred_suspended=len(window.cells)),
        )

    def _add_pending_reason(self, reason: str, count: int = 1) -> None:
        previous = next((item.count for item in self.pending_reasons if item.reason == reason), 0)
        self.pending_reasons = tuple(
            sorted(
                (
                    *(item for item in self.pending_reasons if item.reason != reason),
                    QfqPendingReason(reason, previous + count),
                ),
                key=lambda item: item.reason,
            )
        )

    def summary(self, stage: str, *, level: ProgressLevel = ProgressLevel.NORMAL) -> None:
        percent = self.processed / self.total * 100 if self.total else 100.0
        self.publish(
            stage,
            f"已处理 {ProgressNumber(self.processed)}/{ProgressNumber(self.total)}（{percent:6.2f}%） "
            f"| 合格 {ProgressNumber(self.completed)} | 待重试 {ProgressNumber(self.pending)} "
            f"| 其中复用 {ProgressNumber(self.skipped)} | 无需下载 {ProgressNumber(self.not_applicable)} "
            f"| 无真实日线 {ProgressNumber(self.no_trade_data)} "
            f"| 未处理 {ProgressNumber(self.total - self.processed)} "
            f"| 变更文件 {ProgressNumber(len(self.changed))} | 变更行 {ProgressNumber(self.rows)} "
            f"| 落库耗时 {ProgressDuration(self.write_seconds)}",
            level=level,
        )

    def finish(
        self, cancelled: bool, *, failure_reason: Literal["qfq_worker_unavailable"] | None = None
    ) -> QfqUpdateResult:
        # Unsubmitted stocks remain retryable, distinct from an attempted supplier failure.
        unfinished = self.total - self.processed if cancelled or failure_reason else 0
        if unfinished:
            self._add_pending_reason(
                "cancelled_before_processing" if cancelled else "worker_unavailable_before_processing", unfinished
            )
        result = QfqUpdateResult(
            self.day,
            self.completed,
            self.pending + unfinished,
            tuple(sorted(self.changed)),
            self.rows,
            self.skipped,
            "cancelled" if cancelled else failure_reason,
            self.pending_reasons,
            self.no_trade_data,
            self.latest_missing,
        )
        stage = (
            "已取消"
            if cancelled
            else "结束（仍有待重试）"
            if self.pending or failure_reason
            else "结束（最近日待核）"
            if result.needs_retry
            else "完成"
        )
        level = (
            ProgressLevel.ERROR
            if self.errors or failure_reason
            else ProgressLevel.WARNING
            if cancelled or result.needs_retry
            else ProgressLevel.SUCCESS
        )
        if self.pending_reasons:
            self.publish(
                "待重试原因",
                " | ".join(f"{item.reason} {ProgressNumber(item.count)} 股" for item in self.pending_reasons),
                level=level,
            )
        self.summary(stage, level=level)
        return result
