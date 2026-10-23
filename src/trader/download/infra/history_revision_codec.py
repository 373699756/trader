"""Structured history revision codec retaining exact numeric identity and absent sides."""

from __future__ import annotations

import hashlib
from typing import cast

from trader.download.domain.baostock_daily import BaoStockBoard, BaoStockDailyCell, BaoStockDailySide
from trader.download.domain.history_revision import HistoryRevision
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryCell, PublishedHistorySide
from trader.download.infra.price_columns import PriceCellFromColumns, PriceColumnValues, PriceValues
from trader.infra.serialization.canonical_codec import CanonicalJsonText, CanonicalValue


def RevisionColumnValues(value: HistoryRevision) -> PriceValues:
    return PriceColumnValues(ProjectHistoryCell(value.cell))


def RevisionFromColumns(row: tuple[object, ...]) -> HistoryRevision:
    prices, sequence, board, revision_id, content_hash = _columns(row)
    cell = BaoStockDailyCell(prices.code, prices.trade_date, prices.status, _side(prices.unadjusted), _side(prices.qfq))
    value = HistoryRevision(sequence, board, cell, None, None, None)
    if value.revision_id != revision_id or value.content_hash != content_hash:
        raise ValueError("history_revision_identity_mismatch")
    return value


def PublishedCellFromRevisionColumns(row: tuple[object, ...]) -> PublishedHistoryCell:
    """Verify the same persisted identity without constructing derived side hashes."""
    prices, sequence, board, revision_id, content_hash = _columns(row)
    payload: CanonicalValue = {
        "code": prices.code,
        "trade_date": prices.trade_date.isoformat(),
        "status": prices.status,
        "unadjusted": _side_payload(prices.unadjusted),
        "qfq": _side_payload(prices.qfq),
    }
    encoded = CanonicalJsonText([board, payload]).encode("utf-8")
    # Both persisted arrays contain the identical canonical board/cell payload.
    # Prefix the sequence without traversing or encoding the cell a second time.
    versioned = b"[" + str(sequence).encode("ascii") + b"," + encoded[1:]
    if hashlib.sha256(encoded).hexdigest() != revision_id or hashlib.sha256(versioned).hexdigest() != content_hash:
        raise ValueError("history_revision_identity_mismatch")
    return prices


def _side_payload(side: PublishedHistorySide | None) -> CanonicalValue:
    if side is None:
        return None
    return {
        "code": side.code,
        "trade_date": side.trade_date.isoformat(),
        "adjustment": side.adjustment,
        "open_price": side.open_price,
        "high_price": side.high_price,
        "low_price": side.low_price,
        "close_price": side.close_price,
        "volume": side.volume,
        "amount": side.amount,
        "preclose": side.preclose,
        "pct_change": side.pct_change,
        "turnover": side.turnover,
        "trading_status": side.trading_status,
    }


def _columns(row: tuple[object, ...]) -> tuple[PublishedHistoryCell, int, BaoStockBoard, str, str]:
    if len(row) != 29:
        raise ValueError("history_revision_column_count_invalid")
    day, code, revision_id, sequence, board = row[:5]
    if (
        not all(isinstance(value, str) for value in (day, code, revision_id, board, row[-1]))
        or type(sequence) is not int
        or sequence < 1
        or board not in {"main", "chinext", "star"}
    ):
        raise ValueError("history_revision_fields_invalid")
    prices = PriceCellFromColumns(cast(str, code), cast(str, day), cast(PriceValues, row[5:-1]))
    return prices, sequence, board, cast(str, revision_id), cast(str, row[-1])


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


__all__ = ["RevisionColumnValues", "RevisionFromColumns", "PublishedCellFromRevisionColumns"]
