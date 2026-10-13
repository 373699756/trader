"""Chinese batch feedback for paired-window updates, using one accounting owner."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from trader.download.application.stock_download_progress import (
    StockDownloadProgress,
    format_stock_download_progress,
)
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_supplier_failure import HistorySupplierPendingError
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.domain.qfq_window import QfqUpdateResult, QfqWindowIncompleteError


def qfq_message(elapsed: float, stage: str, details: str) -> str:
    seconds = max(0, int(elapsed))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d} | qfq {stage} | {details}"


def qfq_failure_code(exc: Exception) -> str:
    message = str(exc)
    return message if re.fullmatch(r"[a-zA-Z0-9_]{1,64}", message) else type(exc).__name__


def _window_counts(window: PublishedHistoryWindow) -> tuple[int, int, int]:
    raw = sum(cell.unadjusted is not None for cell in window.cells)
    qfq = sum(cell.qfq is not None for cell in window.cells)
    paired = sum(cell.unadjusted is not None and cell.qfq is not None for cell in window.cells)
    return raw, qfq, paired


@dataclass
class QfqDownloadProgress:
    day: date
    source: str
    total: int
    report: Callable[[str], None]
    elapsed: Callable[[], float]
    completed: int = 0
    pending: int = 0
    skipped: int = 0
    not_applicable: int = 0
    rows: int = 0
    changed: set[str] = field(default_factory=set)

    @property
    def processed(self) -> int:
        return self.completed + self.pending + self.not_applicable

    def publish(self, stage: str, details: str) -> None:
        self.report(qfq_message(self.elapsed(), stage, details))

    def record_stock(
        self,
        security: BaoStockSecurity,
        *,
        action: str,
        result: str,
        target: int,
        raw: int | None,
        qfq: int | None,
        paired: int | None,
        missing_dates: tuple[date, ...] = (),
        reason: str = "无",
    ) -> None:
        self.report(
            format_stock_download_progress(
                self.elapsed(),
                StockDownloadProgress(
                    command="qfq_download",
                    completed=self.processed,
                    total=self.total,
                    code=security.code,
                    name=security.name,
                    action=action,
                    result=result,
                    target=target,
                    raw=raw,
                    qfq=qfq,
                    paired=paired,
                    missing=target - paired if paired is not None else None,
                    missing_dates=missing_dates,
                    reason=reason,
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
    ) -> None:
        self.pending += 1
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
            action=action,
            result="待重试" if action == "Tencent下载" else "失败",
            target=target,
            raw=raw,
            qfq=qfq,
            paired=paired,
            missing_dates=missing_dates,
            reason=qfq_failure_code(exc),
        )

    def summary(self, stage: str) -> None:
        percent = self.processed / self.total * 100 if self.total else 100.0
        self.publish(
            stage,
            f"已处理 {self.processed}/{self.total}（{percent:.2f}%） | 合格 {self.completed} | 待重试 {self.pending} "
            f"| 其中复用 {self.skipped} | 无需下载 {self.not_applicable} | 未处理 {self.total - self.processed} "
            f"| 变更文件 {len(self.changed)} | 变更行 {self.rows}",
        )

    def finish(self, cancelled: bool) -> QfqUpdateResult:
        stage = "已取消" if cancelled else ("结束（仍有待重试）" if self.pending else "完成")
        self.summary(stage)
        # The existing result contract counts unfinished stocks as pending on cancellation.
        # Feedback separates them because they have not actually been processed.
        unfinished = self.total - self.processed if cancelled else 0
        return QfqUpdateResult(
            self.day,
            self.completed,
            self.pending + unfinished,
            tuple(sorted(self.changed)),
            self.rows,
            self.skipped,
            "cancelled" if cancelled else None,
        )
