"""Whole-stock bounded recovery with one BaoStock adjustment basis."""

from __future__ import annotations

from datetime import date

from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.published_history import PublishedHistoryWindow, project_history_cell
from trader.download.infra.baostock_sync_supplier import BaoStockHistorySupplier


class BaoStockQfqRecovery:
    def __init__(self, supplier: BaoStockHistorySupplier, source_identity: str) -> None:
        self._supplier = supplier
        self._source_identity = source_identity

    @property
    def source_identity(self) -> str:
        return self._source_identity

    def fetch_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow:
        if not dates or len(dates) > 251:
            raise ValueError("qfq_recovery_window_out_of_bounds")
        batch = self._supplier.fetch_code(security, dates).batch
        if (
            batch.code != security.code
            or batch.failure_reasons
            or batch.duplicate_rows
            or batch.null_rows
            or batch.out_of_window_rows
            or batch.future_rows
        ):
            raise RuntimeError("qfq_recovery_batch_invalid")
        return PublishedHistoryWindow(batch.code, tuple(project_history_cell(cell) for cell in batch.cells))
