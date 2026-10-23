"""Expose Tencent history prices with BaoStock historical industry references."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_price_qualification import (
    HISTORY_TAIL_CONTRACT,
    HISTORY_UNIVERSE_CONTRACT,
)
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.prepared_reference import PreparedReference
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.infra.baostock_sync_supplier import BaoStockHistorySupplier
from trader.download.infra.history_industry_cache import HistoryIndustryQueryCache
from trader.download.infra.tencent_qfq_supplier import TencentQfqSupplier


class HistorySupplierRouter:
    def __init__(
        self,
        baseline: BaoStockHistorySupplier,
        prices: TencentQfqSupplier,
        reference: PreparedReference,
        industry_cache: HistoryIndustryQueryCache | None = None,
    ) -> None:
        self._baseline = baseline
        self._prices = prices
        self._reference = reference
        self._industry_cache = industry_cache
        self._calendar: tuple[date, ...] = ()

    def load_context(self, as_of: date, sessions: int) -> HistorySupplierContext:
        universe = self._reference.eligible_universe
        price_context = self._prices.load_history_context(
            as_of, sessions, universe, calendar=self._reference.calendar(as_of, sessions)
        )
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
            st_evidence=self._reference.st_evidence,
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
    count = 2000 if dates[0] < calendar[0] else sum(dates[0] <= day <= dates[-1] for day in calendar)
    # Tencent qfq may spend one requested row on the session before the start date.
    request_count = min(2000, max(5, count) + 1)
    return prices.fetch_window(security, dates, request_count=request_count, allow_interior_padding=True)
