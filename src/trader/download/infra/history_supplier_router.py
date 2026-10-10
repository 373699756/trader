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
    repair_tencent_window,
)
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistoryWindow
from trader.download.infra.baostock_sync_supplier import BaoStockHistorySupplier
from trader.download.infra.history_st_source import HistoryStNameSource
from trader.download.infra.tencent_qfq_supplier import TencentQfqSupplier


class HistorySupplierRouter:
    def __init__(
        self,
        baseline: BaoStockHistorySupplier,
        prices: TencentQfqSupplier,
        load_universe: Callable[[], tuple[BaoStockSecurity, ...]],
        st_source: HistoryStNameSource,
    ) -> None:
        self._baseline = baseline
        self._prices = prices
        self._load_universe = load_universe
        self._st_source = st_source
        self._calendar: tuple[date, ...] = ()

    def load_context(self, as_of: date, sessions: int) -> HistorySupplierContext:
        universe = self._load_universe()
        evidence = self._st_source.fetch(universe, as_of)
        clear = {item.code for item in evidence if item.status == "clear"}
        universe = tuple(item for item in universe if item.code in clear)
        if not universe:
            raise RuntimeError("history_st_eligible_universe_empty")
        context = self._baseline.load_context(as_of, sessions, universe=universe)
        self._calendar = context.calendar.open_dates
        versions = context.source_versions
        return replace(
            context,
            st_evidence=evidence,
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
        positions = {day: index for index, day in enumerate(self._calendar)}
        groups: list[list[date]] = []
        for day in dates:
            if not groups or positions[day] != positions[groups[-1][-1]] + 1:
                groups.append([])
            groups[-1].append(day)
        for group in groups:
            start = 0
            overlap = min(5, len(group) - 1)
            while start < len(group):
                end = min(start + TENCENT_HISTORY_MAX_SESSIONS, len(group))
                selected = tuple(group[start:end])
                try:
                    window = self._prices.fetch_window(security, selected)
                except (OSError, RuntimeError, ValueError):
                    window = PublishedHistoryWindow(
                        security.code,
                        tuple(
                            PublishedHistoryCell(security.code, day, "unknown_missing", None, None) for day in selected
                        ),
                    )
                if any(cell.status != "complete" for cell in window.cells):
                    try:
                        window = repair_tencent_window(window, self._prices.fetch_window(security, selected))
                    except (OSError, RuntimeError, ValueError):
                        # Keep qualified first-attempt pairs; the remaining gaps stay explicit.
                        pass
                windows.append(window)
                if end == len(group):
                    break
                start = end - overlap
        return merge_tencent_windows(security.code, dates, tuple(windows))

    def fetch_baostock_prices(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload:
        return self._baseline.fetch_code(security, dates)
