"""Structured history revision codec retaining exact numeric identity and absent sides."""

from __future__ import annotations

from typing import cast

from trader.download.domain.baostock_daily import BaoStockBoard, BaoStockDailyCell, BaoStockDailySide
from trader.download.domain.history_revision import HistoryRevision
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryCell, PublishedHistorySide
from trader.download.infra.price_columns import PriceCellFromColumns, PriceColumnValues, PriceValues
from trader.infra.sqlite_codec import DecodeHash


def RevisionColumnValues(value: HistoryRevision) -> PriceValues:
    return PriceColumnValues(ProjectHistoryCell(value.cell))


def RevisionFromColumns(row: tuple[object, ...]) -> HistoryRevision:
    prices, sequence, board, revision_id, content_hash = _columns(row)
    cell = BaoStockDailyCell(prices.code, prices.trade_date, prices.status, _side(prices.unadjusted), _side(prices.qfq))
    value = HistoryRevision(sequence, board, cell, None, None, None)
    if value.revision_id != revision_id or value.content_hash != content_hash:
        raise ValueError("history_revision_identity_mismatch")
    return value


def PublishedCellFromPriceColumns(row: tuple[object, ...]) -> PublishedHistoryCell:
    """Decode facts already validated by download publication, without rehashing."""
    return PriceCellFromColumns(cast(str, row[1]), cast(int, row[0]), cast(PriceValues, row[2:]), already_validated=True)


def _columns(row: tuple[object, ...]) -> tuple[PublishedHistoryCell, int, BaoStockBoard, str, str]:
    if len(row) != 30:
        raise ValueError("history_revision_column_count_invalid")
    day, code, revision_id, sequence, board = row[:5]
    if (
        not isinstance(code, str)
        or not isinstance(revision_id, str)
        or not isinstance(board, str)
        or type(day) is not int
        or type(sequence) is not int
        or sequence < 1
        or board not in {"main", "chinext", "star"}
    ):
        raise ValueError("history_revision_fields_invalid")
    prices = PriceCellFromColumns(code, day, cast(PriceValues, row[5:-1]))
    return prices, sequence, cast(BaoStockBoard, board), revision_id, DecodeHash(row[-1])


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


__all__ = ["RevisionColumnValues", "RevisionFromColumns", "PublishedCellFromPriceColumns"]
