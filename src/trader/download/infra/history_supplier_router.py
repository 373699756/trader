"""Expose Tencent-first history fields and serialized BaoStock supplementation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date

from trader.download.domain.baostock_daily import BaoStockCodeDownload, BaoStockSecurity
from trader.download.domain.history_price_qualification import (
    HISTORY_TAIL_CONTRACT,
    HISTORY_UNIVERSE_CONTRACT,
    TENCENT_HISTORY_MAX_SESSIONS,
    merge_tencent_windows,
)
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.infra.baostock_sync_supplier import BaoStockHistorySupplier
from trader.download.infra.tencent_qfq_supplier import TencentQfqSupplier


class HistorySupplierRouter:
    def __init__(
        self,
        baseline: BaoStockHistorySupplier,
        prices: TencentQfqSupplier,
        load_universe: Callable[[], tuple[BaoStockSecurity, ...]],
    ) -> None:
        self._baseline = baseline
        self._prices = prices
        self._load_universe = load_universe

    def load_context(self, as_of: date, sessions: int) -> HistorySupplierContext:
        universe = self._load_universe()
        context = self._baseline.load_context(as_of, sessions, universe=universe)
        versions = context.source_versions
        return replace(
            context,
            source_versions=replace(
                versions,
                sdk_version=f"{versions.sdk_version}+tencent-history",
                dependency_versions=(
                    *versions.dependency_versions,
                    HISTORY_TAIL_CONTRACT,
                    HISTORY_UNIVERSE_CONTRACT,
                ),
            ),
        )

    def fetch_tencent_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow:
        if not dates or dates != tuple(sorted(set(dates))):
            raise ValueError("Tencent history dates must be non-empty, ordered, and unique")
        windows = []
        start = 0
        overlap = min(5, len(dates) - 1)
        while start < len(dates):
            end = min(start + TENCENT_HISTORY_MAX_SESSIONS, len(dates))
            windows.append(self._prices.fetch_window(security, dates[start:end]))
            if end == len(dates):
                break
            start = end - overlap
        return merge_tencent_windows(security.code, dates, tuple(windows))

    def fetch_baostock_raw(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload:
        return self._baseline.fetch_raw_code(security, dates)

    def fetch_baostock_prices(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload:
        return self._baseline.fetch_code(security, dates)
