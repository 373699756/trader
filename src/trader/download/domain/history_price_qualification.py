"""Keep existing valid daily pairs and fill gaps; unknown never means suspended."""

from __future__ import annotations

from datetime import date
from math import isclose

from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockDailySide
from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistorySide, PublishedHistoryWindow

HISTORY_TAIL_CONTRACT = ("history", "tencent-actual-rows-inferred-suspension")
HISTORY_UNIVERSE_CONTRACT = ("security-universe", "exchange-current-never-st")


def _require_raw_overlap(previous: BaoStockDailyCell, incoming: BaoStockDailyCell) -> None:
    before, after = previous.unadjusted, incoming.unadjusted
    if before is None or after is None or before.trading_status != after.trading_status:
        raise RuntimeError("history_tail_raw_price_conflict")
    if before.trading_status != "suspended":
        _require_raw_prices(before, after)


def QualifyPriceOverlap(
    cells: tuple[BaoStockDailyCell, ...], previous: tuple[BaoStockDailyCell, ...], *, full_window: bool
) -> tuple[BaoStockDailyCell, ...]:
    """Check raw anchors but preserve the entire previously qualified daily pair."""
    prior = {cell.trade_date: cell for cell in previous}
    anchored = full_window
    for cell in cells:
        before = prior.get(cell.trade_date)
        if before is not None and before.unadjusted is not None and cell.unadjusted is not None:
            _require_raw_overlap(before, cell)
            anchored = True
        elif before is not None and before.status == "unadjusted_missing" and cell.status == "complete":
            anchored = True
    result = []
    for cell in cells:
        before = prior.get(cell.trade_date)
        if before is not None and before.obtained and before.status != "tencent_inferred_suspended":
            result.append(before)
        elif (
            before is not None
            and before.status in {"qfq_missing", "unadjusted_missing", "tencent_inferred_suspended"}
            and (cell.status != "complete" or not anchored)
        ):
            result.append(before)
        elif (
            anchored or not cell.obtained or cell.status in {"supplier_marked_suspended", "tencent_inferred_suspended"}
        ):
            result.append(cell)
        else:
            result.append(BaoStockDailyCell(cell.code, cell.trade_date, "unknown_missing", None, None))
    return tuple(result)


def QualifyHistoryPairs(
    code: str,
    dates: tuple[date, ...],
    tencent: PublishedHistoryWindow,
    recovery: tuple[BaoStockDailyCell, ...] = (),
) -> tuple[BaoStockDailyCell, ...]:
    if tencent.code != code or any(cell.code != code or cell.trade_date not in dates for cell in recovery):
        raise RuntimeError("history_supplier_identity_conflict")
    if len({cell.trade_date for cell in recovery}) != len(recovery):
        raise RuntimeError("history_supplier_duplicate_dates")
    candidates = {cell.trade_date: cell for cell in tencent.cells}
    if any(day not in dates for day in candidates):
        raise RuntimeError("history_tencent_identity_conflict")
    supplements = {cell.trade_date: cell for cell in recovery}
    for day, cell in supplements.items():
        candidate = candidates.get(day)
        if candidate is not None and candidate.unadjusted is not None and candidate.qfq is not None:
            _require_recovery_anchor(candidate, cell)
    result = []
    for day in dates:
        candidate = candidates.get(day)
        if candidate is not None and candidate.status in {
            "complete",
            "supplier_marked_suspended",
            "tencent_inferred_suspended",
            "qfq_missing",
            "unadjusted_missing",
        }:
            result.append(
                BaoStockDailyCell(
                    code,
                    day,
                    candidate.status,
                    _side(candidate.unadjusted) if candidate.unadjusted is not None else None,
                    _side(candidate.qfq) if candidate.qfq is not None else None,
                )
            )
        else:
            recovered = supplements.get(day)
            result.append(
                recovered
                if recovered is not None and recovered.obtained
                else BaoStockDailyCell(code, day, "unknown_missing", None, None)
            )
    return tuple(result)


def _require_recovery_anchor(candidate: PublishedHistoryCell, recovery: BaoStockDailyCell) -> None:
    raw, qfq = recovery.unadjusted, recovery.qfq
    if raw is None or qfq is None or raw.trading_status != "trading" or qfq.trading_status != "trading":
        raise RuntimeError("history_recovery_anchor_missing")
    assert candidate.unadjusted is not None and candidate.qfq is not None
    # Complete existing pairs win; recovery supplies missing dates, not qfq revisions.
    _require_raw_prices(candidate.unadjusted, raw)


def _require_raw_prices(
    left: PublishedHistorySide | BaoStockDailySide, right: PublishedHistorySide | BaoStockDailySide
) -> None:
    for a, b, tolerance in (
        (left.open_price, right.open_price, 0.0051),
        (left.high_price, right.high_price, 0.0051),
        (left.low_price, right.low_price, 0.0051),
        (left.close_price, right.close_price, 0.0051),
        (left.volume, right.volume, 100.0),
        (left.amount, right.amount, 100.0),
    ):
        if a is None or b is None or not isclose(a, b, rel_tol=0.0, abs_tol=tolerance):
            raise RuntimeError("history_tail_raw_price_conflict")


def _side(side: PublishedHistorySide | None) -> BaoStockDailySide:
    assert side is not None
    return BaoStockDailySide(
        side.code,
        side.trade_date,
        side.adjustment,
        side.open_price,
        side.high_price,
        side.low_price,
        side.close_price,
        side.volume,
        side.amount,
        side.preclose,
        side.pct_change,
        side.turnover,
        side.trading_status,
    )
