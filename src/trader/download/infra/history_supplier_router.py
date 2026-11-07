"""Bind disclosed CAPCO industry intervals to independently qualified price history."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
import logging

from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.historical_industry import HistoricalIndustryPreparation
from trader.download.domain.history_price_qualification import (
    HISTORY_TAIL_CONTRACT,
    HISTORY_UNIVERSE_CONTRACT,
)
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.prepared_reference import PreparedReference
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryWindow
from trader.download.domain.price_review import PriceReviewRequestBudget
from trader.download.infra.baostock_sync_supplier import BaoStockHistorySupplier
from trader.download.infra.history_industry_preparation import PrepareHistoricalIndustries
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
        industry_preparation: Callable[
            [tuple[BaoStockSecurity, ...], tuple[date, ...], datetime], HistoricalIndustryPreparation
        ] | None = None,
        progress: HistorySyncProgressPort | None = None,
        cancel_requested: Callable[[], bool] = lambda: False,
    ) -> None:
        self._baseline = baseline
        self._prices = prices
        self._reference = reference
        self._industry_cache = industry_cache
        self._industry_preparation = industry_preparation
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
            if self._cancel_requested():
                raise RuntimeError("supplier_cancelled")
            if self._industry_preparation is not None:
                industries = self._industry_preparation(universe, context.calendar.open_dates, self._reference.checked_at)
            else:
                industries = PrepareHistoricalIndustries(
                    self._industry_cache,
                    universe,
                    context.calendar.open_dates,
                    self._reference.checked_at,
                    cancel_requested=self._cancel_requested,
                    progress=self._progress,
                )
            if self._cancel_requested():
                raise RuntimeError("supplier_cancelled")
        except (OSError, RuntimeError, ValueError) as exc:
            if self._cancel_requested() or str(exc) == "supplier_cancelled":
                raise
            _LOGGER.warning("historical_industry_unavailable category=%s", type(exc).__name__, exc_info=True)
            industries = HistoricalIndustryPreparation(
                (), HistoryIndustryReadiness(len(universe), 0, 0, source="capco", failure_category=type(exc).__name__),
            )
            if self._progress is not None:
                self._progress.publish(HistorySyncProgress(
                    "supplier_industry", "failed", 0, len(universe), "capco", failure_detail=type(exc).__name__,
                    data_source="本地行业证据" if self._industry_preparation is None else None,
                ))
        self._calendar = context.calendar.open_dates
        versions = context.source_versions
        return replace(
            context,
            industry_intervals=industries.intervals,
            st_evidence=self._reference.st_evidence,
            reference_id=self._reference.identity,
            industry_readiness=industries.readiness,
            source_versions=replace(
                versions,
                sdk_version=f"{versions.sdk_version}+capco-industry",
                dependency_versions=(
                    *versions.dependency_versions,
                    ("capco_industry", industries.data_version),
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
