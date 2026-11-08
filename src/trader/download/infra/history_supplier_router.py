"""Route qualified price history through prepared calendar and eligibility references."""

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
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryWindow
from trader.download.domain.price_review import PriceReviewRequestBudget
from trader.download.infra.baostock_sync_supplier import BaoStockHistorySupplier
from collections.abc import Callable

from trader.download.infra.tencent_qfq_supplier import TencentQfqSupplier

class HistorySupplierRouter:
    def __init__(
        self,
        baseline: BaoStockHistorySupplier,
        prices: TencentQfqSupplier,
        reference: PreparedReference,
        *,
        cancel_requested: Callable[[], bool] = lambda: False,
    ) -> None:
        self._baseline = baseline
        self._prices = prices
        self._reference = reference
        self._cancel_requested = cancel_requested
        self._calendar: tuple[date, ...] = ()

    def load_context(self, as_of: date, sessions: int) -> HistorySupplierContext:
        if self._cancel_requested():
            raise RuntimeError("supplier_cancelled")
        universe = self._reference.eligible_universe
        price_context = self._prices.load_history_context(
            as_of, sessions, universe, calendar=self._reference.calendar(as_of, sessions)
        )
        if self._cancel_requested():
            raise RuntimeError("supplier_cancelled")
        self._calendar = price_context.calendar.open_dates
        versions = price_context.source_versions
        return replace(
            price_context,
            industry_intervals=(),
            st_evidence=self._reference.st_evidence,
            reference_id=self._reference.identity,
            industry_readiness=None,
            source_versions=replace(
                versions,
                dependency_versions=(
                    *versions.dependency_versions,
                    ("tencent_calendar", price_context.source_versions.content_hash),
                    HISTORY_TAIL_CONTRACT,
                    HISTORY_UNIVERSE_CONTRACT,
                    ("price-repair", "whole-window"),
                ),
            ),
        )

    def fetch_tencent_window(
        self,
        security: BaoStockSecurity,
        dates: tuple[date, ...],
        *,
        request_budget: PriceReviewRequestBudget | None = None,
    ) -> PublishedHistoryWindow:
        return FetchTencentHistoryWindow(self._prices, security, dates, self._calendar, request_budget=request_budget)

    def fetch_baostock_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow:
        result = self._baseline.fetch_code(security, dates)
        return PublishedHistoryWindow(security.code, tuple(ProjectHistoryCell(cell) for cell in result.batch.cells))


def FetchTencentHistoryWindow(
    prices: TencentQfqSupplier,
    security: BaoStockSecurity,
    dates: tuple[date, ...],
    calendar: tuple[date, ...],
    *,
    request_budget: PriceReviewRequestBudget | None = None,
) -> PublishedHistoryWindow:
    if not dates or dates != tuple(sorted(set(dates))):
        raise ValueError("Tencent history dates must be non-empty, ordered, and unique")
    if not calendar or any(day >= calendar[0] and day not in calendar for day in dates):
        raise ValueError("Tencent history dates must belong to the exchange calendar")
    count = 2000 if dates[0] < calendar[0] else sum(dates[0] <= day <= dates[-1] for day in calendar)
    # Tencent qfq may spend two requested rows on sessions before the start date.
    request_count = min(2000, max(7, count + 2))
    if request_budget is None:
        return prices.fetch_window(security, dates, request_count=request_count, allow_interior_padding=True)
    return prices.fetch_window(
        security,
        dates,
        request_count=request_count,
        allow_interior_padding=True,
        request_budget=request_budget,
    )
