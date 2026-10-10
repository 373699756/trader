"""Choose suppliers by the actual per-security requested history window."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from trader.download.domain.baostock_daily import BaoStockCodeDownload, BaoStockSecurity
from trader.download.domain.history_price_qualification import (
    HISTORY_TAIL_CONTRACT,
    TENCENT_HISTORY_MAX_SESSIONS,
    HistoryBaselineSupplier,
    HistoryPriceSupplier,
    qualify_history_tail,
)
from trader.download.domain.history_sync import HistorySupplierContext, HistorySyncProgress, HistorySyncProgressPort


class HistorySupplierRouter:
    def __init__(
        self,
        baseline: HistoryBaselineSupplier,
        prices: HistoryPriceSupplier,
        *,
        progress: HistorySyncProgressPort | None = None,
    ) -> None:
        self._baseline = baseline
        self._prices = prices
        self._progress = progress

    def load_context(self, as_of: date, sessions: int) -> HistorySupplierContext:
        context = self._baseline.load_context(as_of, sessions)
        versions = context.source_versions
        return replace(
            context,
            source_versions=replace(
                versions,
                sdk_version=f"{versions.sdk_version}+tencent-history-tail",
                dependency_versions=(*versions.dependency_versions, HISTORY_TAIL_CONTRACT),
            ),
        )

    def fetch_code(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload:
        if not dates or dates != tuple(sorted(set(dates))):
            raise ValueError("history request dates must be non-empty ordered and unique")
        short = len(dates) <= TENCENT_HISTORY_MAX_SESSIONS
        if self._progress is not None:
            self._progress.publish(
                HistorySyncProgress(
                    "supplier_routing",
                    "started",
                    0,
                    1,
                    current_item=security.code,
                    supplier_source="tencent" if short else "baostock",
                    requested_sessions=len(dates),
                )
            )
        if not short:
            return self._baseline.fetch_code(security, dates)
        prices = self._prices.fetch_window(security, dates)
        evidence = self._baseline.fetch_raw_code(security, dates)
        return qualify_history_tail(security, dates, prices, evidence)
