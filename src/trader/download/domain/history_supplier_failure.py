"""A temporary supplier failure carries validated partial data, never fallback permission."""

from trader.download.domain.published_history import PublishedHistoryWindow


class HistorySupplierPendingError(RuntimeError):
    def __init__(self, window: PublishedHistoryWindow) -> None:
        super().__init__("tencent_history_pending")
        self.window = window


__all__ = ["HistorySupplierPendingError"]
