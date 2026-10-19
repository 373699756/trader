"""Structured history revision codec retaining exact numeric identity and absent sides."""

from __future__ import annotations

from typing import cast

from trader.download.domain.baostock_daily import BaoStockBoard, BaoStockDailyCell, BaoStockDailySide
from trader.download.domain.history_revision import HistoryRevision
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistorySide
from trader.download.infra.price_columns import PriceCellFromColumns, PriceColumnValues, PriceValues


def RevisionColumnValues(value: HistoryRevision) -> PriceValues:
    return PriceColumnValues(ProjectHistoryCell(value.cell))


def RevisionFromColumns(row: tuple[object, ...]) -> HistoryRevision:
    if len(row) != 29:
        raise ValueError("history_revision_column_count_invalid")
    day, code, revision_id, sequence, board = row[:5]
    if (
        not all(isinstance(value, str) for value in (day, code, revision_id, board, row[-1]))
        or type(sequence) is not int
    ):
        raise ValueError("history_revision_fields_invalid")
    prices = PriceCellFromColumns(cast(str, code), cast(str, day), cast(PriceValues, row[5:-1]))
    cell = BaoStockDailyCell(prices.code, prices.trade_date, prices.status, _side(prices.unadjusted), _side(prices.qfq))
    value = HistoryRevision(sequence, cast(BaoStockBoard, board), cell, None, None, None)
    if value.revision_id != revision_id or value.content_hash != row[-1]:
        raise ValueError("history_revision_identity_mismatch")
    return value


def _side(value: PublishedHistorySide | None) -> BaoStockDailySide | None:
    if value is None:
        return None
    return BaoStockDailySide(
        value.code,
        value.trade_date,
        value.adjustment,
        value.open_price,
        value.high_price,
        value.low_price,
        value.close_price,
        value.volume,
        value.amount,
        value.preclose,
        value.pct_change,
        value.turnover,
        value.trading_status,
    )


__all__ = ["RevisionColumnValues", "RevisionFromColumns"]
