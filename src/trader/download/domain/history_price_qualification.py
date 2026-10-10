"""Qualify history price pairs with genuine daily metadata."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from math import isclose
from typing import Protocol

from trader.download.domain.baostock_daily import (
    BaoStockCodeBatch,
    BaoStockCodeDownload,
    BaoStockDailyCell,
    BaoStockDailySide,
    BaoStockSecurity,
)
from trader.download.domain.history_sync import HistorySyncSupplier
from trader.download.domain.published_history import PublishedHistorySide, PublishedHistoryWindow

TENCENT_HISTORY_MAX_SESSIONS = 640
HISTORY_TAIL_CONTRACT = ("history_tail", "tencent-pair-baostock-raw")


class HistoryBaselineSupplier(HistorySyncSupplier, Protocol):
    def fetch_raw_code(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload: ...


class HistoryPriceSupplier(Protocol):
    def fetch_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow: ...


def qualify_history_tail(
    security: BaoStockSecurity,
    dates: tuple[date, ...],
    prices: PublishedHistoryWindow,
    evidence: BaoStockCodeDownload,
) -> BaoStockCodeDownload:
    """Never infer historical ST, suspension, or missing trading-day prices."""
    batch = evidence.batch
    if (
        prices.code != security.code
        or batch.code != security.code
        or tuple(cell.trade_date for cell in prices.cells) != dates
        or tuple(cell.trade_date for cell in batch.cells) != dates
        or batch.duplicate_rows
        or batch.null_rows
        or batch.out_of_window_rows
        or batch.future_rows
        or batch.failure_reasons
    ):
        raise RuntimeError("history_tail_evidence_incomplete")
    cells: list[BaoStockDailyCell] = []
    for price, fact in zip(prices.cells, batch.cells, strict=True):
        raw = fact.unadjusted
        if raw is None:
            raise RuntimeError("history_tail_raw_metadata_missing")
        if raw.trading_status == "suspended":
            # Only the explicit daily supplier fact authorizes a suspended cell.
            if price.unadjusted is not None or price.qfq is not None:
                raise RuntimeError("history_tail_trading_status_conflict")
            qfq = BaoStockDailySide(
                security.code,
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
        else:
            if price.unadjusted is None or price.qfq is None:
                raise RuntimeError("history_tail_tencent_pair_missing")
            _require_raw_parity(raw, price.unadjusted)
            raw = replace(
                raw,
                open_price=price.unadjusted.open_price,
                high_price=price.unadjusted.high_price,
                low_price=price.unadjusted.low_price,
                close_price=price.unadjusted.close_price,
                volume=price.unadjusted.volume,
                amount=price.unadjusted.amount,
            )
            side = price.qfq
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
                side.trading_status,
            )
        cells.append(
            BaoStockDailyCell(
                security.code,
                raw.trade_date,
                "supplier_marked_suspended" if raw.trading_status == "suspended" else "complete",
                raw,
                qfq,
            )
        )
    return BaoStockCodeDownload(BaoStockCodeBatch(security.code, tuple(cells)), evidence.daily_facts)


def _require_raw_parity(raw: BaoStockDailySide, price: PublishedHistorySide) -> None:
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
