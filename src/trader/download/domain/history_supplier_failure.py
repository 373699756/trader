"""A temporary supplier failure carries validated partial data, never fallback permission."""

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
        self, window: PublishedHistoryWindow, reason: HistorySupplierPendingReason = "tencent_history_pending"
    ) -> None:
        super().__init__(reason)
        self.window = window
        self.reason = reason


__all__ = ["HISTORY_SUPPLIER_PENDING_REASONS", "HistorySupplierPendingError", "HistorySupplierPendingReason"]
