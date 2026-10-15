"""Expose Tencent history prices with BaoStock historical industry references."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date

from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_price_qualification import (
    HISTORY_TAIL_CONTRACT,
    HISTORY_UNIVERSE_CONTRACT,
)
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.infra.baostock_sync_supplier import BaoStockHistorySupplier
from trader.download.infra.exchange_security_universe import SelectStEligibleUniverse
from trader.download.infra.history_industry_cache import HistoryIndustryQueryCache
from trader.download.infra.history_st_source import HistoryStNameSource
from trader.download.infra.tencent_qfq_supplier import TencentQfqSupplier


class HistorySupplierRouter:
    def __init__(
        self,
        baseline: BaoStockHistorySupplier,
        prices: TencentQfqSupplier,
        load_universe: Callable[[], tuple[BaoStockSecurity, ...]],
        st_source: HistoryStNameSource,
        industry_cache: HistoryIndustryQueryCache | None = None,
    ) -> None:
        self._baseline = baseline
        self._prices = prices
        self._load_universe = load_universe
        self._st_source = st_source
        self._industry_cache = industry_cache
        self._calendar: tuple[date, ...] = ()

    def load_context(self, as_of: date, sessions: int) -> HistorySupplierContext:
        universe = self._load_universe()
        evidence = self._st_source.fetch(universe, as_of)
        universe = SelectStEligibleUniverse(universe, evidence)
        price_context = self._prices.load_history_context(as_of, sessions, universe)
        try:
            context = self._baseline.load_industry_context(
                price_context.calendar,
                universe,
                industry_cache=self._industry_cache,
            )
        except RuntimeError as exc:
            if str(exc) != "supplier_login_timeout":
                raise
            context = price_context
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
                    ("tencent_calendar", price_context.source_versions.content_hash),
                    HISTORY_TAIL_CONTRACT,
                    HISTORY_UNIVERSE_CONTRACT,
                ),
            ),
        )

    def fetch_tencent_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow:
        return FetchTencentHistoryWindow(self._prices, security, dates, self._calendar)


def FetchTencentHistoryWindow(
    prices: TencentQfqSupplier, security: BaoStockSecurity, dates: tuple[date, ...], calendar: tuple[date, ...]
) -> PublishedHistoryWindow:
    if not dates or dates != tuple(sorted(set(dates))):
        raise ValueError("Tencent history dates must be non-empty, ordered, and unique")
    if not calendar or any(day >= calendar[0] and day not in calendar for day in dates):
        raise ValueError("Tencent history dates must belong to the exchange calendar")
    return prices.fetch_window(security, dates, request_count=2000, allow_interior_padding=True)
