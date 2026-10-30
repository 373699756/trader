"""Structured daily prices shared by the download persistence adapters."""

from __future__ import annotations

import math
from datetime import date
from typing import TypeAlias, cast

from trader.download.domain.baostock_daily import BaoStockAdjustment, BaoStockCellStatus, BaoStockTradingStatus
from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistorySide
from trader.infra.sqlite_codec import DecodeDate

PriceValues: TypeAlias = tuple[str | float | int | None, ...]
_FIELDS = ("open", "high", "low", "close", "volume", "amount", "preclose", "change_pct", "turnover_pct")
_NUMBER_POSITIONS = (*range(2, 11), *range(13, 22))
PRICE_COLUMNS = (
    ("status",)
    + tuple(f"{side}_{field}" for side in ("raw", "qfq") for field in ("present", *_FIELDS, "trading_status"))
    + ("number_flags",)
)


def _price_sql(*, database_checks: bool) -> str:
    return ",".join(
        f"{name} "
        + (
            "INTEGER NOT NULL CHECK(number_flags>=0 AND number_flags<68719476736)"
            if name == "number_flags"
            else "TEXT"
            if name == "status" or name.endswith("trading_status")
            else "INTEGER"
            if name.endswith("present")
            else f"REAL CHECK(typeof({name}) IN ('real','null'))"
            if database_checks
            else "REAL"
        )
        for name in PRICE_COLUMNS
    )


PRICE_SQL = _price_sql(database_checks=True)
# PublishedHistoryCell validates writes; qfq identity hashes and PriceCellFromColumns validate reads.
PRICE_WINDOW_SQL = _price_sql(database_checks=False)


def PriceColumnValues(cell: PublishedHistoryCell) -> PriceValues:
    values = (cell.status, *_side_values(cell.unadjusted), *_side_values(cell.qfq))
    flags = 0
    for bit, position in enumerate(_NUMBER_POSITIONS):
        value = values[position]
        if type(value) is int:
            if int(float(value)) != value:
                raise ValueError("price_integer_precision_invalid")
            flags |= 1 << bit
        elif type(value) is float and value == 0 and math.copysign(1, value) < 0:
            flags |= 1 << (bit + 18)
    return (*values, flags)


def PriceCellFromColumns(
    code: str, day: int, values: PriceValues, *, already_validated: bool = False
) -> PublishedHistoryCell:
    if not already_validated and (len(values) != len(PRICE_COLUMNS) or not isinstance(values[0], str)):
        raise ValueError("price_columns_invalid")
    trade_date = DecodeDate(day)
    values = DecodePriceValues(values)
    return PublishedHistoryCell(
        code,
        trade_date,
        cast(BaoStockCellStatus, values[0]),
        _side(code, trade_date, "unadjusted", values[1:12], already_validated=already_validated),
        _side(code, trade_date, "qfq", values[12:23], already_validated=already_validated),
        already_validated=already_validated,
    )


def DecodePriceValues(values: PriceValues) -> PriceValues:
    """Keep original integer types and negative zero across SQLite REAL affinity."""
    if len(values) != len(PRICE_COLUMNS) or type(values[-1]) is not int:
        raise ValueError("price_number_flags_invalid")
    flags = values[-1]
    if not 0 <= flags < 1 << 36 or flags & (flags >> 18):
        raise ValueError("price_number_flags_invalid")
    if flags == 0:
        return values[:-1]
    decoded = list(values[:-1])
    while flags:
        lowest = flags & -flags
        bit = lowest.bit_length() - 1
        flags ^= lowest
        position = _NUMBER_POSITIONS[bit % 18]
        value = decoded[position]
        if type(value) not in (float, int) or not math.isfinite(value) or int(value) != value:
            raise ValueError("price_number_flags_invalid")
        if bit >= 18 and value != 0:
            raise ValueError("price_number_flags_invalid")
        decoded[position] = int(value) if bit < 18 else -0.0
    return tuple(decoded)


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


def _side(
    code: str, day: date, adjustment: BaoStockAdjustment, values: PriceValues, *, already_validated: bool
) -> PublishedHistorySide | None:
    if values[0] == 0 and (already_validated or all(value is None for value in values[1:])):
        return None
    if not already_validated:
        if values[0] != 1 or values[-1] not in ("trading", "suspended"):
            raise ValueError("price_side_invalid")
        for value in values[1:10]:
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value)
            ):
                raise ValueError("price_number_invalid")
    numbers = cast(tuple[float | None, ...], values[1:10])
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
        already_validated=already_validated,
    )


__all__ = [
    "PRICE_COLUMNS",
    "PRICE_SQL",
    "PRICE_WINDOW_SQL",
    "PriceValues",
    "PriceColumnValues",
    "PriceCellFromColumns",
    "DecodePriceValues",
]
