"""Chinese batch feedback for paired-window updates, using one accounting owner."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.qfq_window import QfqUpdateResult, QfqWindowIncompleteError


def qfq_message(elapsed: float, stage: str, details: str) -> str:
    seconds = max(0, int(elapsed))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d} | qfq {stage} | {details}"


def _pending_reason(exc: Exception) -> tuple[str, str]:
    message = str(exc)
    reason = message if re.fullmatch(r"[a-zA-Z0-9_]{1,64}", message) else type(exc).__name__
    incomplete = re.fullmatch(r"qfq_incomplete_raw_(\d+)_qfq_(\d+)", reason)
    if incomplete:
        details = f"未复权缺 {int(incomplete[1])} 日、前复权缺 {int(incomplete[2])} 日"
        if isinstance(exc, QfqWindowIncompleteError):
            details += f"，Tencent未返回 {_format_missing_dates(exc)}"
        return reason, details
    return reason, f"处理失败（{reason}）"


def _format_missing_dates(exc: QfqWindowIncompleteError) -> str:
    days = tuple(sorted(set(exc.raw_missing) | set(exc.qfq_missing)))
    preview = ", ".join(day.isoformat() for day in days[:8])
    if len(days) > 8:
        preview += f" 等 {len(days)} 个交易日"
    return preview or "交易日"


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
    pending_details: list[str] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return self.completed + self.pending + self.not_applicable

    def publish(self, stage: str, details: str) -> None:
        self.report(qfq_message(self.elapsed(), stage, details))

    def begin_batch(self, securities: tuple[BaoStockSecurity, ...]) -> None:
        names = "、".join(f"{item.code} {item.name}" for item in securities)
        self.publish("下载", f"本批 {len(securities)} 只 | 股票 {names}")

    def record_pending(self, security: BaoStockSecurity, exc: Exception) -> None:
        self.pending += 1
        reason, description = _pending_reason(exc)
        self.pending_details.append(f"{security.code} {security.name}，{description} | reason={reason}")

    def summary(self, stage: str) -> None:
        percent = self.processed / self.total * 100 if self.total else 100.0
        self.publish(
            stage,
            f"已处理 {self.processed}/{self.total}（{percent:.2f}%）| 合格 {self.completed} | 待补 {self.pending} "
            f"| 其中复用 {self.skipped} | 无需下载 {self.not_applicable} | 未处理 {self.total - self.processed} "
            f"| 变更文件 {len(self.changed)}",
        )
        self.flush_pending()

    def flush_pending(self) -> None:
        for details in self.pending_details:
            self.publish("待补", details)
        self.pending_details.clear()

    def finish(self, cancelled: bool) -> QfqUpdateResult:
        stage = "已取消" if cancelled else ("结束（仍有待补）" if self.pending else "完成")
        self.flush_pending()
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
