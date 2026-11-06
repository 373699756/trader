"""Expose Tencent history and independently inspect CNINFO industry evidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
import logging

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
from trader.download.infra.history_industry_preparation import InspectHistoricalIndustryQueries
from trader.download.infra.industry_query_index import SQLiteIndustryQueryIndex
from trader.download.domain.history_sync import HistorySyncProgressPort
from trader.download.domain.history_sync import HistoryIndustryReadiness, HistorySyncProgress
from collections.abc import Callable

from trader.download.infra.tencent_qfq_supplier import TencentQfqSupplier

_LOGGER = logging.getLogger(__name__)


class HistorySupplierRouter:
    def __init__(
        self,
        baseline: BaoStockHistorySupplier,
        prices: TencentQfqSupplier,
        reference: PreparedReference,
        industry_cache: SQLiteIndustryQueryIndex | None = None,
        *,
        progress: HistorySyncProgressPort | None = None,
        cancel_requested: Callable[[], bool] = lambda: False,
    ) -> None:
        self._baseline = baseline
        self._prices = prices
        self._reference = reference
        self._industry_cache = industry_cache
        self._progress = progress
        self._cancel_requested = cancel_requested
        self._calendar: tuple[date, ...] = ()

    def load_context(self, as_of: date, sessions: int) -> HistorySupplierContext:
        universe = self._reference.eligible_universe
        price_context = self._prices.load_history_context(
            as_of, sessions, universe, calendar=self._reference.calendar(as_of, sessions)
        )
        context = price_context
        try:
            readiness = InspectHistoricalIndustryQueries(
                self._industry_cache, universe, context.calendar.open_dates[0], context.calendar.open_dates[-1],
                self._reference.checked_at, cancel_requested=self._cancel_requested, progress=self._progress,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            if self._cancel_requested() or str(exc) == "supplier_cancelled":
                raise
            _LOGGER.warning("historical_industry_unavailable category=%s", type(exc).__name__, exc_info=True)
            readiness = HistoryIndustryReadiness(len(universe), 0, 0, failure_category=type(exc).__name__)
            if self._progress is not None:
                self._progress.publish(HistorySyncProgress(
                    "supplier_industry", "failed", 0, len(universe), "cninfo", failure_detail=type(exc).__name__,
                ))
        self._calendar = context.calendar.open_dates
        versions = context.source_versions
        return replace(
            context,
            st_evidence=self._reference.st_evidence,
            reference_id=self._reference.identity,
            industry_readiness=readiness,
            source_versions=replace(
                versions,
                sdk_version=f"{versions.sdk_version}+cninfo-industry",
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
