"""Qualify history price pairs with genuine daily metadata."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from math import isclose

from trader.download.domain.baostock_daily import (
    BaoStockCodeBatch,
    BaoStockCodeDownload,
    BaoStockDailyCell,
    BaoStockDailySide,
    BaoStockSecurity,
)
from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistorySide, PublishedHistoryWindow

TENCENT_HISTORY_MAX_SESSIONS = 640
HISTORY_TAIL_CONTRACT = ("history", "tencent-first-baostock-gap")


def _require_raw_parity(raw: BaoStockDailySide, price: PublishedHistorySide | BaoStockDailySide) -> None:
    comparisons = (
        (raw.open_price, price.open_price, 0.0051),
        (raw.high_price, price.high_price, 0.0051),
        (raw.low_price, price.low_price, 0.0051),
        (raw.close_price, price.close_price, 0.0051),
        (raw.volume, price.volume, 100.0),
        (raw.amount, price.amount, 100.0),
    )
    if price.trading_status != raw.trading_status or any(
        left is None or right is None or not isclose(left, right, rel_tol=0.0, abs_tol=tolerance)
        for left, right, tolerance in comparisons
    ):
        raise RuntimeError("history_tail_raw_price_conflict")


def require_history_qfq_overlap(previous: BaoStockDailyCell, incoming: BaoStockDailyCell) -> None:
    """Accept vendor price precision, reject an incompatible adjustment basis."""
    before, after = previous.qfq, incoming.qfq
    if before is None or after is None or before.trading_status != after.trading_status:
        raise RuntimeError("history_tail_qfq_basis_conflict")
    if before.trading_status == "suspended":
        return
    if any(
        left is None or right is None or not isclose(left, right, rel_tol=0.0, abs_tol=0.0051)
        for left, right in (
            (before.open_price, after.open_price),
            (before.high_price, after.high_price),
            (before.low_price, after.low_price),
            (before.close_price, after.close_price),
        )
    ):
        raise RuntimeError("history_tail_qfq_basis_conflict")


def merge_tencent_windows(
    code: str,
    dates: tuple[date, ...],
    windows: tuple[PublishedHistoryWindow, ...],
) -> PublishedHistoryWindow:
    """Merge bounded Tencent segments while checking their overlapping dates."""
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
    before = previous
    after = incoming
    for left, right in ((before.unadjusted, after.unadjusted), (before.qfq, after.qfq)):
        if (left is None) != (right is None):
            raise RuntimeError("history_tencent_overlap_conflict")
        if left is None or right is None:
            continue
        if any(
            value_left is None
            or value_right is None
            or not isclose(value_left, value_right, rel_tol=0.0, abs_tol=0.0051)
            for value_left, value_right in (
                (left.open_price, right.open_price),
                (left.high_price, right.high_price),
                (left.low_price, right.low_price),
                (left.close_price, right.close_price),
            )
        ):
            raise RuntimeError("history_tencent_overlap_conflict")


def combine_history_sources(
    security: BaoStockSecurity,
    dates: tuple[date, ...],
    tencent: PublishedHistoryWindow,
    metadata: BaoStockCodeDownload,
    price_gaps: BaoStockCodeDownload | None,
) -> BaoStockCodeDownload:
    """Fill Tencent candidates from BaoStock without cross-vendor daily pairs."""
    if tencent.code != security.code or any(cell.trade_date not in dates for cell in tencent.cells):
        raise RuntimeError("history_tencent_identity_conflict")
    _validate_baostock_batches(security.code, dates, metadata, price_gaps)
    candidate_by_date = {cell.trade_date: cell for cell in tencent.cells}
    fallback_by_date = {cell.trade_date: cell for cell in price_gaps.batch.cells} if price_gaps is not None else {}
    _require_supplement_overlap(candidate_by_date, fallback_by_date)
    cells = tuple(
        _combine_history_day(security.code, facts, candidate_by_date.get(day), fallback_by_date.get(day))
        for day, facts in zip(dates, metadata.batch.cells, strict=True)
    )
    return BaoStockCodeDownload(BaoStockCodeBatch(security.code, cells), metadata.daily_facts)


def _validate_baostock_batches(
    code: str,
    dates: tuple[date, ...],
    metadata: BaoStockCodeDownload,
    price_gaps: BaoStockCodeDownload | None,
) -> None:
    baseline = metadata.batch
    if (
        baseline.code != code
        or tuple(cell.trade_date for cell in baseline.cells) != dates
        or baseline.duplicate_rows
        or baseline.null_rows
        or baseline.out_of_window_rows
        or baseline.future_rows
        or baseline.failure_reasons
    ):
        raise RuntimeError("history_baostock_metadata_incomplete")
    if price_gaps is not None and (
        price_gaps.batch.code != code
        or price_gaps.batch.duplicate_rows
        or price_gaps.batch.null_rows
        or price_gaps.batch.out_of_window_rows
        or price_gaps.batch.future_rows
        or price_gaps.batch.failure_reasons
        or any(cell.trade_date not in dates for cell in price_gaps.batch.cells)
    ):
        raise RuntimeError("history_baostock_price_gap_incomplete")


def _combine_history_day(
    code: str,
    facts: BaoStockDailyCell,
    candidate: PublishedHistoryCell | None,
    fallback: BaoStockDailyCell | None,
) -> BaoStockDailyCell:
    raw_facts = facts.unadjusted
    if raw_facts is None:
        raise RuntimeError("history_baostock_metadata_incomplete")
    if raw_facts.trading_status == "suspended":
        if candidate is not None and (candidate.unadjusted is not None or candidate.qfq is not None):
            raise RuntimeError("history_trading_status_conflict")
        return _suspended_cell(code, raw_facts)
    if candidate is None or candidate.unadjusted is None or candidate.qfq is None:
        return _qualify_baostock_pair(code, raw_facts, fallback)
    _require_raw_parity(raw_facts, candidate.unadjusted)
    raw = replace(
        raw_facts,
        open_price=candidate.unadjusted.open_price,
        high_price=candidate.unadjusted.high_price,
        low_price=candidate.unadjusted.low_price,
        close_price=candidate.unadjusted.close_price,
        volume=candidate.unadjusted.volume,
        amount=candidate.unadjusted.amount,
    )
    side = candidate.qfq
    qfq = BaoStockDailySide(
        side.code,
        side.trade_date,
        "qfq",
        side.open_price,
        side.high_price,
        side.low_price,
        side.close_price,
        side.volume,
        side.amount,
        None,
        None,
        None,
        raw.trading_status,
    )
    return BaoStockDailyCell(code, facts.trade_date, "complete", raw, qfq)


def _qualify_baostock_pair(
    code: str,
    raw_facts: BaoStockDailySide,
    fallback: BaoStockDailyCell | None,
) -> BaoStockDailyCell:
    if fallback is None or not fallback.obtained or fallback.unadjusted is None or fallback.qfq is None:
        raise RuntimeError("history_baostock_price_gap_incomplete")
    _require_raw_parity(raw_facts, fallback.unadjusted)
    return BaoStockDailyCell(code, raw_facts.trade_date, "complete", fallback.unadjusted, fallback.qfq)


def _require_supplement_overlap(
    candidates: dict[date, PublishedHistoryCell],
    supplementation: dict[date, BaoStockDailyCell],
) -> None:
    for day, fallback in supplementation.items():
        candidate = candidates.get(day)
        if candidate is None:
            continue
        if candidate.unadjusted is not None:
            if fallback.unadjusted is None:
                raise RuntimeError("history_baostock_price_gap_incomplete")
            _require_raw_parity(fallback.unadjusted, candidate.unadjusted)
        if candidate.qfq is not None:
            _require_published_qfq_parity(candidate.qfq, fallback.qfq)


def _suspended_cell(code: str, raw: BaoStockDailySide) -> BaoStockDailyCell:
    qfq = BaoStockDailySide(
        code,
        raw.trade_date,
        "qfq",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        "suspended",
    )
    return BaoStockDailyCell(code, raw.trade_date, "supplier_marked_suspended", raw, qfq)


def _require_published_qfq_parity(candidate: PublishedHistorySide, fallback: BaoStockDailySide | None) -> None:
    if fallback is None or candidate.trading_status != fallback.trading_status:
        raise RuntimeError("history_tail_qfq_basis_conflict")
    if candidate.trading_status == "suspended":
        return
    if any(
        left is None or right is None or not isclose(left, right, rel_tol=0.0, abs_tol=0.0051)
        for left, right in (
            (candidate.open_price, fallback.open_price),
            (candidate.high_price, fallback.high_price),
            (candidate.low_price, fallback.low_price),
            (candidate.close_price, fallback.close_price),
        )
    ):
        raise RuntimeError("history_tail_qfq_basis_conflict")
