"""Pending supplier observations carry validated partial data, never fallback permission."""

from datetime import date
from typing import Literal

from trader.download.domain.published_history import PublishedHistoryWindow

HistorySupplierPendingReason = Literal[
    "tencent_history_pending",
    "tencent_qfq_invalid_price",
    "tencent_qfq_page_request_failed",
    "tencent_qfq_page_invalid",
    "tencent_qfq_anchor_conflict",
    "tencent_qfq_page_stalled",
    "tencent_qfq_page_limit",
]

HISTORY_SUPPLIER_PENDING_REASONS = frozenset(
    (
        "tencent_history_pending",
        "tencent_qfq_invalid_price",
        "tencent_qfq_page_request_failed",
        "tencent_qfq_page_invalid",
        "tencent_qfq_anchor_conflict",
        "tencent_qfq_page_stalled",
        "tencent_qfq_page_limit",
    )
)


class HistorySupplierPendingError(RuntimeError):
    def __init__(
        self,
        window: PublishedHistoryWindow,
        reason: HistorySupplierPendingReason = "tencent_history_pending",
        unusable_qfq_dates: tuple[date, ...] = (),
    ) -> None:
        cells = {cell.trade_date: cell for cell in window.cells}
        if unusable_qfq_dates != tuple(sorted(set(unusable_qfq_dates))) or any(
            day not in cells or cells[day].qfq is not None for day in unusable_qfq_dates
        ):
            raise ValueError("history_supplier_unusable_qfq_dates_invalid")
        super().__init__(reason)
        self.window = window
        self.reason = reason
        self.unusable_qfq_dates = unusable_qfq_dates


__all__ = ["HISTORY_SUPPLIER_PENDING_REASONS", "HistorySupplierPendingError", "HistorySupplierPendingReason"]
