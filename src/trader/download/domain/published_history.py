"""Immutable identities exposed by the published history read boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from trader.download.domain.baostock_daily import (
    BaoStockAdjustment,
    BaoStockCellStatus,
    BaoStockDailyCell,
    BaoStockDailySide,
    BaoStockTradingStatus,
    daily_cell_status,
    validate_daily_side_values,
)


@dataclass(frozen=True, slots=True)
class PublishedHistorySide:
    """Daily facts from an already verified partition, without derived hashes."""

    code: str
    trade_date: date
    adjustment: BaoStockAdjustment
    open_price: float | None
    high_price: float | None
    low_price: float | None
    close_price: float | None
    volume: float | None
    amount: float | None
    preclose: float | None
    pct_change: float | None
    turnover: float | None
    trading_status: BaoStockTradingStatus

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit() or type(self.trade_date) is not date:
            raise ValueError("published history side identity is invalid")
        validate_daily_side_values(
            self.adjustment,
            self.trading_status,
            (self.open_price, self.high_price, self.low_price, self.close_price),
            (self.volume, self.amount),
            (self.preclose, self.pct_change, self.turnover),
        )


@dataclass(frozen=True, slots=True)
class PublishedHistoryCell:
    code: str
    trade_date: date
    status: BaoStockCellStatus
    unadjusted: PublishedHistorySide | None
    qfq: PublishedHistorySide | None

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit() or type(self.trade_date) is not date:
            raise ValueError("published history cell code is invalid")
        for side, adjustment in ((self.unadjusted, "unadjusted"), (self.qfq, "qfq")):
            if side is not None and (
                side.code != self.code or side.trade_date != self.trade_date or side.adjustment != adjustment
            ):
                raise ValueError("published history side identity is invalid")
        expected = daily_cell_status(self.unadjusted, self.qfq)
        if self.status != expected:
            raise ValueError("published history cell status is invalid")


def project_history_cell(cell: BaoStockDailyCell) -> PublishedHistoryCell:
    """Project validated supplier facts with exactly the same overlap semantics."""
    return PublishedHistoryCell(
        cell.code, cell.trade_date, cell.status, _project_side(cell.unadjusted), _project_side(cell.qfq)
    )


def _project_side(side: BaoStockDailySide | None) -> PublishedHistorySide | None:
    if side is None:
        return None
    return PublishedHistorySide(
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


@dataclass(frozen=True, slots=True)
class PublishedHistoryManifest:
    snapshot_hash: str
    sequence: int
    data_cutoff: date
    calendar_dates: tuple[date, ...]
    universe_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        dates = tuple(self.calendar_dates)
        codes = tuple(self.universe_codes)
        if (
            len(self.snapshot_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.snapshot_hash)
            or self.sequence < 1
            or not dates
            or dates != tuple(sorted(set(dates)))
            or dates[-1] != self.data_cutoff
            or not codes
            or codes != tuple(sorted(set(codes)))
            or any(len(code) != 6 or not code.isdigit() for code in codes)
        ):
            raise ValueError("published history manifest is invalid")
        object.__setattr__(self, "calendar_dates", dates)
        object.__setattr__(self, "universe_codes", codes)


@dataclass(frozen=True, slots=True)
class PublishedHistoryWindow:
    code: str
    cells: tuple[PublishedHistoryCell, ...]

    def __post_init__(self) -> None:
        cells = tuple(self.cells)
        dates = tuple(item.trade_date for item in cells)
        if (
            len(self.code) != 6
            or not self.code.isdigit()
            or any(item.code != self.code for item in cells)
            or dates != tuple(sorted(set(dates)))
        ):
            raise ValueError("published history window is invalid")
        object.__setattr__(self, "cells", cells)


__all__ = [
    "PublishedHistoryCell",
    "PublishedHistoryManifest",
    "PublishedHistorySide",
    "PublishedHistoryWindow",
    "project_history_cell",
]
