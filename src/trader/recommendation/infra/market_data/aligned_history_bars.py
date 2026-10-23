"""Adapt verified published facts to calendar-aligned recommendation history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from trader.download.domain.published_history import PublishedHistoryWindow
from trader.infra.market_data.history.history import DailyBar, PriceAdjustment
from trader.recommendation.domain.market.history_alignment import (
    AlignedMarketSession,
    AlignMarketSession,
    MarketDailyObservation,
    SessionKind,
)


@dataclass(frozen=True, slots=True)
class AlignedHistoryBars:
    bars: tuple[DailyBar, ...]
    estimated_dates: tuple[date, ...]
    suspended_dates: tuple[date, ...]
    latest_observed_date: date | None


def BuildAlignedHistoryBars(
    window: PublishedHistoryWindow,
    calendar: tuple[date, ...],
    lifetime: tuple[date, date | None] | None = None,
) -> AlignedHistoryBars:
    """Retain only the contiguous suffix after an unrepairable observed row."""
    cells = {cell.trade_date: cell for cell in window.cells}
    if len(cells) != len(window.cells):
        raise ValueError("history_alignment_duplicate_row")
    bars: list[DailyBar] = []
    estimated: list[date] = []
    suspended: list[date] = []
    previous: AlignedMarketSession | None = None
    latest_observed_date: date | None = None
    for day in calendar:
        if lifetime is not None and (day < lifetime[0] or (lifetime[1] is not None and day >= lifetime[1])):
            continue
        cell = cells.get(day)
        side = cell.qfq if cell else None
        raw = cell.unadjusted if cell else None
        kind: SessionKind = (
            "estimated"
            if cell is None or cell.status in {"unknown_missing", "tencent_inferred_suspended"}
            else "suspended"
            if cell.status == "supplier_marked_suspended"
            else "trading"
            if cell.status == "complete"
            else "invalid"
        )
        observation = MarketDailyObservation(
            day,
            side.close_price if side else None,
            side.amount if side else None,
            side.open_price if side else None,
            side.high_price if side else None,
            side.low_price if side else None,
            raw.volume if raw else None,
            kind,
        )
        point = AlignMarketSession(day, observation, previous)
        if point is None or any(v is None for v in (point.open_price, point.high, point.low, point.volume)):
            bars.clear()
            estimated.clear()
            suspended.clear()
            previous = None
            latest_observed_date = None
            continue
        assert point.open_price is not None and point.high is not None and point.low is not None
        assert point.volume is not None
        bars.append(
            DailyBar(
                day.isoformat(),
                point.open_price,
                point.close,
                point.high,
                point.low,
                point.volume,
                point.amount,
                (point.close / previous.close - 1.0) * 100.0 if previous else 0.0,
                raw.turnover if raw else None,
                adjustment=PriceAdjustment.QFQ,
                source="published_history",
            )
        )
        if point.kind == "estimated":
            estimated.append(day)
        elif point.kind == "suspended":
            suspended.append(day)
        if point.label_available:
            latest_observed_date = day
        previous = point
    return AlignedHistoryBars(tuple(bars), tuple(estimated), tuple(suspended), latest_observed_date)


__all__ = ["AlignedHistoryBars", "BuildAlignedHistoryBars"]
