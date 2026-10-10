"""Qualify same-source daily price pairs; an unknown gap never means suspended."""

from __future__ import annotations

from datetime import date
from math import isclose

from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockDailySide
from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistorySide, PublishedHistoryWindow

TENCENT_HISTORY_MAX_SESSIONS = 640
HISTORY_TAIL_CONTRACT = ("history", "tencent-pairs-sparse-recovery")
HISTORY_UNIVERSE_CONTRACT = ("security-universe", "exchange-current-never-st")


def require_history_qfq_overlap(previous: BaoStockDailyCell, incoming: BaoStockDailyCell) -> None:
    before, after = previous.qfq, incoming.qfq
    if before is None or after is None or before.trading_status != after.trading_status:
        raise RuntimeError("history_tail_qfq_basis_conflict")
    if before.trading_status != "suspended":
        _require_qfq_prices(before, after)


def _require_qfq_prices(
    left: PublishedHistorySide | BaoStockDailySide, right: PublishedHistorySide | BaoStockDailySide
) -> None:
    if any(
        a is None or b is None or not isclose(a, b, rel_tol=0.0, abs_tol=0.0051)
        for a, b in zip(
            (left.open_price, left.high_price, left.low_price, left.close_price),
            (right.open_price, right.high_price, right.low_price, right.close_price),
            strict=True,
        )
    ):
        raise RuntimeError("history_tail_qfq_basis_conflict")


def merge_tencent_windows(
    code: str, dates: tuple[date, ...], windows: tuple[PublishedHistoryWindow, ...]
) -> PublishedHistoryWindow:
    cells: dict[date, PublishedHistoryCell] = {}
    for window in windows:
        if window.code != code:
            raise RuntimeError("history_tencent_code_conflict")
        for incoming in window.cells:
            previous = cells.get(incoming.trade_date)
            if previous is not None:
                _require_published_overlap(previous, incoming)
            cells[incoming.trade_date] = incoming
    if tuple(sorted(cells)) != dates:
        raise RuntimeError("history_tencent_coverage_incomplete")
    return PublishedHistoryWindow(code, tuple(cells[day] for day in dates))


def _require_published_overlap(previous: PublishedHistoryCell, incoming: PublishedHistoryCell) -> None:
    for left, right in ((previous.unadjusted, incoming.unadjusted), (previous.qfq, incoming.qfq)):
        if (left is None) != (right is None):
            raise RuntimeError("history_tencent_overlap_conflict")
        if left is not None and right is not None:
            _require_qfq_prices(left, right)


def repair_tencent_window(previous: PublishedHistoryWindow, retry: PublishedHistoryWindow) -> PublishedHistoryWindow:
    if previous.code != retry.code or tuple(cell.trade_date for cell in previous.cells) != tuple(
        cell.trade_date for cell in retry.cells
    ):
        raise RuntimeError("history_tencent_retry_identity_conflict")
    cells = []
    for before, after in zip(previous.cells, retry.cells, strict=True):
        for left, right in ((before.unadjusted, after.unadjusted), (before.qfq, after.qfq)):
            if left is not None and right is not None:
                _require_qfq_prices(left, right)
        cells.append(before if before.status == "complete" else after if after.status == "complete" else before)
    return PublishedHistoryWindow(previous.code, tuple(cells))


def qualify_history_pairs(
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
        if candidate is not None and candidate.status == "complete":
            result.append(BaoStockDailyCell(code, day, "complete", _side(candidate.unadjusted), _side(candidate.qfq)))
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
    _require_qfq_prices(candidate.qfq, qfq)
    for a, b, tolerance in (
        (raw.open_price, candidate.unadjusted.open_price, 0.0051),
        (raw.high_price, candidate.unadjusted.high_price, 0.0051),
        (raw.low_price, candidate.unadjusted.low_price, 0.0051),
        (raw.close_price, candidate.unadjusted.close_price, 0.0051),
        (raw.volume, candidate.unadjusted.volume, 100.0),
        (raw.amount, candidate.unadjusted.amount, 100.0),
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
