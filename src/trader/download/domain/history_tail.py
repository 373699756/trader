"""Bounded, immutable supplier windows for candidate history recovery."""

from dataclasses import dataclass
from datetime import date

from trader.download.domain.baostock_daily import BaoStockDailyCell


@dataclass(frozen=True, slots=True)
class HistoryTailRequest:
    code: str
    dates: tuple[date, ...]

    def __post_init__(self) -> None:
        if (
            len(self.code) != 6
            or not self.code.isdigit()
            or not 4 <= len(self.dates) <= 8
            or any(type(day) is not date for day in self.dates)
            or self.dates != tuple(sorted(set(self.dates)))
        ):
            raise ValueError("history_tail_request_invalid")


@dataclass(frozen=True, slots=True)
class HistoryTailWindow:
    code: str
    source: str
    cells: tuple[BaoStockDailyCell, ...]

    def __post_init__(self) -> None:
        dates = tuple(cell.trade_date for cell in self.cells)
        if (
            len(self.code) != 6
            or not self.code.isdigit()
            or not self.source.strip()
            or len(self.cells) > 8
            or any(cell.code != self.code for cell in self.cells)
            or dates != tuple(sorted(set(dates)))
        ):
            raise ValueError("history_tail_response_invalid")
