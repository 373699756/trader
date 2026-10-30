"""Immutable identities exposed by the published history read boundary."""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field
from datetime import date

from trader.download.domain.baostock_daily import (
    BaoStockAdjustment,
    BaoStockCellStatus,
    BaoStockDailyCell,
    BaoStockDailySide,
    BaoStockTradingStatus,
    DailyCellStatus,
    ValidateDailyPrices,
    ValidateDailyRawFields,
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
    already_validated: InitVar[bool] = field(default=False, kw_only=True)

    def __post_init__(self, already_validated: bool) -> None:
        if already_validated:
            return
        if len(self.code) != 6 or not self.code.isdigit() or type(self.trade_date) is not date:
            raise ValueError("published history side identity is invalid")
        ValidateDailyPrices(
            self.adjustment,
            self.trading_status,
            (self.open_price, self.high_price, self.low_price, self.close_price),
            (self.volume, self.amount),
        )
        ValidateDailyRawFields(
            self.adjustment,
            self.trading_status,
            (self.preclose, self.pct_change, self.turnover),
            require_raw_fields=False,
        )


@dataclass(frozen=True, slots=True)
class PublishedHistoryCell:
    code: str
    trade_date: date
    status: BaoStockCellStatus
    unadjusted: PublishedHistorySide | None
    qfq: PublishedHistorySide | None
    already_validated: InitVar[bool] = field(default=False, kw_only=True)

    def __post_init__(self, already_validated: bool) -> None:
        if already_validated:
            return
        if len(self.code) != 6 or not self.code.isdigit() or type(self.trade_date) is not date:
            raise ValueError("published history cell code is invalid")
        for side, adjustment in ((self.unadjusted, "unadjusted"), (self.qfq, "qfq")):
            if side is not None and (
                side.code != self.code or side.trade_date != self.trade_date or side.adjustment != adjustment
            ):
                raise ValueError("published history side identity is invalid")
        expected = (
            "tencent_inferred_suspended"
            if self.status == "tencent_inferred_suspended" and self.unadjusted is None and self.qfq is None
            else DailyCellStatus(self.unadjusted, self.qfq)
        )
        if self.status != expected:
            raise ValueError("published history cell status is invalid")


def ProjectHistoryCell(cell: BaoStockDailyCell) -> PublishedHistoryCell:
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
    """Content identity plus an optional commit token for mutable window publications."""

    snapshot_hash: str
    sequence: int
    data_cutoff: date
    calendar_dates: tuple[date, ...]
    universe_codes: tuple[str, ...]
    publication_id: str | None = None
    listing_intervals: tuple[tuple[str, date, date | None], ...] = ()

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
            or len({code for code, _, _ in self.listing_intervals}) != len(self.listing_intervals)
            or any(
                code not in codes or (end is not None and end < start) for code, start, end in self.listing_intervals
            )
            or self.publication_id is not None
            and (
                len(self.publication_id) != 32
                or any(character not in "0123456789abcdef" for character in self.publication_id)
            )
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
    "ProjectHistoryCell",
]
