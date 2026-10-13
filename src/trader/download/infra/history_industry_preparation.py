"""Prepare dated industry evidence before opening a supplier session."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from itertools import groupby

from trader.download.domain.baostock_daily import BaoStockCalendar, BaoStockIndustryInterval, BaoStockSecurity
from trader.download.domain.history_sync import HistorySyncProgress
from trader.download.infra.history_industry_cache import HistoryIndustryQueryCache


def prepare_history_industries(
    calendar: BaoStockCalendar,
    universe: tuple[BaoStockSecurity, ...],
    source: str,
    fetch: Callable[[date], tuple[BaoStockIndustryInterval, ...]],
    *,
    cache: HistoryIndustryQueryCache | None = None,
    progress: Callable[[HistorySyncProgress], None] | None = None,
    cancel_requested: Callable[[], bool] = lambda: False,
) -> tuple[BaoStockIndustryInterval, ...]:
    snapshots = _industry_snapshot_dates(calendar.open_dates)
    as_of = calendar.open_dates[-1]
    if cache is not None:
        cache.prepare(source, snapshots)
    codes = frozenset(item.code for item in universe)
    observations: list[BaoStockIndustryInterval] = []
    for index, snapshot_date in enumerate(snapshots):
        if cancel_requested():
            raise RuntimeError("supplier_cancelled")
        if progress is not None:
            progress(HistorySyncProgress("supplier_industry", "started", index, len(snapshots), str(snapshot_date)))
        facts = cache.read(source, snapshot_date, as_of) if cache is not None else None
        expected_codes = frozenset(
            item.code
            for item in universe
            if item.listed_on <= snapshot_date and (item.delisted_on is None or snapshot_date < item.delisted_on)
        )
        # The provider query is market-wide. Missing classifications are evidence
        # gaps, not evidence that a successfully consumed query must be repeated.
        reused = facts is not None
        if facts is None:
            facts = fetch(snapshot_date)
            if cancel_requested():
                raise RuntimeError("supplier_cancelled")
            if cache is not None:
                cache.save(source, snapshot_date, as_of, facts)
        observations.extend(fact for fact in facts if fact.code in codes)
        if progress is not None:
            progress(
                HistorySyncProgress(
                    "supplier_industry",
                    "completed",
                    index + 1,
                    len(snapshots),
                    str(snapshot_date),
                    cache_hit=reused,
                    industry_missing_codes=len(expected_codes - {fact.code for fact in facts}),
                )
            )
    return _industry_intervals(observations)


def _industry_intervals(observations: list[BaoStockIndustryInterval]) -> tuple[BaoStockIndustryInterval, ...]:
    intervals: list[BaoStockIndustryInterval] = []
    for code, facts in groupby(sorted(observations, key=lambda item: item.code), key=lambda item: item.code):
        ordered = tuple(sorted({(fact.effective_from, fact.industry, fact.classification) for fact in facts}))
        for left, right in zip(ordered, ordered[1:], strict=False):
            if left[0] == right[0] and left[1:] != right[1:]:
                raise ValueError("BaoStock industry facts conflict at the same effective date")
        compressed: list[tuple[date, str, str]] = []
        for value in ordered:
            if not compressed or value[1:] != compressed[-1][1:]:
                compressed.append(value)
        for index, (effective_from, industry, classification) in enumerate(compressed):
            effective_to = compressed[index + 1][0] if index + 1 < len(compressed) else None
            intervals.append(BaoStockIndustryInterval(code, effective_from, effective_to, industry, classification))
    return tuple(sorted(intervals, key=lambda item: (item.code, item.effective_from)))


def _industry_snapshot_dates(open_dates: tuple[date, ...]) -> tuple[date, ...]:
    snapshots = [open_dates[0]]
    for day in open_dates[1:-1]:
        previous = snapshots[-1]
        if (day.year, (day.month - 1) // 6) != (previous.year, (previous.month - 1) // 6):
            snapshots.append(day)
    if open_dates[-1] != snapshots[-1]:
        snapshots.append(open_dates[-1])
    if len(snapshots) > 64:
        raise ValueError("industry_cache_dates_invalid")
    return tuple(snapshots)


__all__ = ["prepare_history_industries"]
