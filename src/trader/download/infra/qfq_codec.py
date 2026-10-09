"""Compact canonical SQLite payload codec, confined to the persistence boundary."""

from __future__ import annotations

import json
from datetime import date
from typing import cast

from trader.download.domain.baostock_daily import BaoStockAdjustment, BaoStockCellStatus, BaoStockTradingStatus
from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistorySide


def encode_cell(cell: PublishedHistoryCell) -> str:
    return json.dumps(
        [cell.status, _side_values(cell.unadjusted), _side_values(cell.qfq)],
        separators=(",", ":"),
        allow_nan=False,
    )


def decode_cell(code: str, day: str, payload: str) -> PublishedHistoryCell:
    value: object = json.loads(payload)
    if not isinstance(value, list) or len(value) != 3 or not isinstance(value[0], str):
        raise ValueError("qfq cell payload invalid")
    trade_date = date.fromisoformat(day)
    return PublishedHistoryCell(
        code,
        trade_date,
        cast(BaoStockCellStatus, value[0]),
        _decode_side(code, trade_date, "unadjusted", value[1]),
        _decode_side(code, trade_date, "qfq", value[2]),
    )


def _side_values(side: PublishedHistorySide | None) -> list[float | str | None] | None:
    if side is None:
        return None
    return [
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
    ]


def _decode_side(code: str, day: date, adjustment: BaoStockAdjustment, value: object) -> PublishedHistorySide | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 10 or value[-1] not in ("trading", "suspended"):
        raise ValueError("qfq side payload invalid")
    numbers: list[float | None] = []
    for number in value[:9]:
        if number is not None and (isinstance(number, bool) or not isinstance(number, (float, int))):
            raise ValueError("qfq numeric payload invalid")
        numbers.append(None if number is None else float(number))
    return PublishedHistorySide(
        code,
        day,
        adjustment,
        numbers[0],
        numbers[1],
        numbers[2],
        numbers[3],
        numbers[4],
        numbers[5],
        numbers[6],
        numbers[7],
        numbers[8],
        cast(BaoStockTradingStatus, value[-1]),
    )
