"""Expose Tencent-first history fields and serialized BaoStock supplementation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date

from trader.download.domain.baostock_daily import BaoStockCodeDownload, BaoStockSecurity
from trader.download.domain.history_price_qualification import (
    HISTORY_TAIL_CONTRACT,
    HISTORY_UNIVERSE_CONTRACT,
)
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.history_trading_status import HistoryTradingFact
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.infra.baostock_sync_supplier import BaoStockHistorySupplier
from trader.download.infra.exchange_security_universe import select_st_eligible_universe
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
        universe = select_st_eligible_universe(universe, evidence)
        context = self._baseline.load_context(as_of, sessions, universe=universe, industry_cache=self._industry_cache)
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
        return fetch_tencent_history_window(self._prices, security, dates, self._calendar)

    def fetch_baostock_prices(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload:
        return self._baseline.fetch_code(security, dates)

    def fetch_trading_facts(
        self, security: BaoStockSecurity, dates: tuple[date, ...]
    ) -> tuple[HistoryTradingFact, ...]:
        return self._baseline.fetch_trading_facts(security, dates)


def fetch_tencent_history_window(
    prices: TencentQfqSupplier, security: BaoStockSecurity, dates: tuple[date, ...], calendar: tuple[date, ...]
) -> PublishedHistoryWindow:
    if not dates or dates != tuple(sorted(set(dates))):
        raise ValueError("Tencent history dates must be non-empty, ordered, and unique")
    if not calendar or any(day >= calendar[0] and day not in calendar for day in dates):
        raise ValueError("Tencent history dates must belong to the exchange calendar")
    return prices.fetch_window(security, dates, request_count=2000, allow_interior_padding=True)
