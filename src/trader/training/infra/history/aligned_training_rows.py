"""Adapt verified training facts to the shared market-calendar alignment contract."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import replace
from datetime import date
from itertools import groupby

from trader.download.domain.history_revision import HistoryTrainingPoint, HistoryTrainingRow
from trader.recommendation.domain.market.history_alignment import (
    AlignedMarketSession,
    AlignMarketSession,
    MarketDailyObservation,
    SessionKind,
)


def AlignmentReadDates(calendar: Sequence[date], requested: frozenset[date]) -> frozenset[date]:
    """Include bounded earlier facts to seed a leading no-trade interval causally."""
    if not requested:
        return frozenset()
    cutoff = max(requested)
    return frozenset(day for day in calendar[-2000:] if day <= cutoff)


def AlignTrainingRows(
    rows: Iterable[HistoryTrainingRow],
    calendar: Sequence[date],
    listing_intervals: tuple[tuple[str, date, date | None], ...] = (),
) -> Iterator[HistoryTrainingRow]:
    """Keep absent sessions in each known stock's timeline without future filling.

    A wholly absent source day is a source gap, not evidence of a market-wide
    suspension. Explicit unavailable cells remain distinguishable from absent rows.
    """
    groups = iter(groupby(rows, key=lambda row: row.trade_date))
    current = next(groups, None)
    previous: dict[str, AlignedMarketSession] = {}
    facts: dict[str, HistoryTrainingRow] = {}
    lifetimes = {code: (start, end) for code, start, end in listing_intervals}
    for day in calendar:
        if current is None:
            break
        if current[0] < day:
            raise ValueError("history_alignment_calendar_mismatch")
        if current[0] != day:
            if facts:
                raise ValueError("history_alignment_source_day_missing")
            continue
        observed: dict[str, HistoryTrainingRow] = {}
        for observed_row in current[1]:
            if observed_row.code in observed:
                raise ValueError("history_alignment_duplicate_row")
            observed[observed_row.code] = observed_row
        for code in sorted(facts.keys() | observed.keys()):
            lifetime = lifetimes.get(code)
            if lifetime is not None and (day < lifetime[0] or (lifetime[1] is not None and day >= lifetime[1])):
                previous.pop(code, None)
                facts.pop(code, None)
                continue
            row = observed.get(code)
            if row is None:
                row = replace(facts[code], trade_date=day, point=None, cell_status="unknown_missing")
            aligned = AlignMarketSession(day, _observation(row), previous.get(code))
            if aligned is not None:
                previous[code] = aligned
                point = HistoryTrainingPoint(
                    day,
                    aligned.close,
                    aligned.amount,
                    aligned.open_price,
                    aligned.high,
                    aligned.low,
                    aligned.volume,
                )
            else:
                previous.pop(code, None)
                point = None
            facts[code] = row
            yield replace(
                row,
                point=point,
                cell_status="unknown_missing"
                if aligned is not None and aligned.kind == "estimated"
                else row.cell_status,
            )
        current = next(groups, None)
    if current is not None:
        raise ValueError("history_alignment_row_outside_calendar")


def _observation(row: HistoryTrainingRow) -> MarketDailyObservation:
    kind: SessionKind = (
        "suspended"
        if row.cell_status == "supplier_marked_suspended"
        else "estimated"
        if row.cell_status in {"tencent_inferred_suspended", "unknown_missing"}
        else "trading"
        if row.cell_status in {None, "complete"}
        else "invalid"
    )
    point = row.point
    return MarketDailyObservation(
        row.trade_date,
        point.qfq_close_price if point else None,
        point.qfq_amount if point else None,
        point.qfq_open if point else None,
        point.qfq_high if point else None,
        point.qfq_low if point else None,
        point.raw_volume if point else None,
        kind,
    )


__all__ = ["AlignTrainingRows", "AlignmentReadDates"]
