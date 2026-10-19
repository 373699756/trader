"""Structured daily prices shared by the download persistence adapters."""

from __future__ import annotations

import math
from datetime import date
from typing import TypeAlias, cast

from trader.download.domain.baostock_daily import BaoStockAdjustment, BaoStockCellStatus, BaoStockTradingStatus
from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistorySide

PriceValues: TypeAlias = tuple[str | float | int | None, ...]
_FIELDS = ("open", "high", "low", "close", "volume", "amount", "preclose", "pct_change", "turnover")
PRICE_COLUMNS = ("status",) + tuple(
    f"{side}_{field}" for side in ("raw", "qfq") for field in ("present", *_FIELDS, "trading_status")
)
PRICE_SQL = ",".join(
    f"{name} "
    + (
        "TEXT"
        if name == "status" or name.endswith("trading_status")
        else "INTEGER"
        if name.endswith("present")
        else f"BLOB CHECK(typeof({name}) IN ('integer','real','null'))"
    )
    for name in PRICE_COLUMNS
)


def PriceColumnValues(cell: PublishedHistoryCell) -> PriceValues:
    return (cell.status, *_side_values(cell.unadjusted), *_side_values(cell.qfq))


def PriceCellFromColumns(code: str, day: str, values: PriceValues) -> PublishedHistoryCell:
    if len(values) != len(PRICE_COLUMNS) or not isinstance(values[0], str):
        raise ValueError("price_columns_invalid")
    trade_date = date.fromisoformat(day)
    return PublishedHistoryCell(
        code,
        trade_date,
        cast(BaoStockCellStatus, values[0]),
        _side(code, trade_date, "unadjusted", values[1:12]),
        _side(code, trade_date, "qfq", values[12:23]),
    )


def _side_values(side: PublishedHistorySide | None) -> PriceValues:
    if side is None:
        return (0, *(None for _ in range(10)))
    return (
        1,
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


def _side(code: str, day: date, adjustment: BaoStockAdjustment, values: PriceValues) -> PublishedHistorySide | None:
    if values[0] == 0 and all(value is None for value in values[1:]):
        return None
    if values[0] != 1 or values[-1] not in ("trading", "suspended"):
        raise ValueError("price_side_invalid")
    numbers: list[float | None] = []
    for value in values[1:10]:
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value)
        ):
            raise ValueError("price_number_invalid")
        numbers.append(value)
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
        cast(BaoStockTradingStatus, values[-1]),
    )


__all__ = ["PRICE_COLUMNS", "PRICE_SQL", "PriceValues", "PriceColumnValues", "PriceCellFromColumns"]
