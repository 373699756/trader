"""Explicit supplier trading facts; absent bars never establish suspension."""

from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockDailySide


@dataclass(frozen=True)
class HistoryTradingFact:
    code: str
    trade_date: date
    status: Literal["trading", "suspended"]
    is_st: bool
    source: Literal["baostock"] = "baostock"

    def __post_init__(self) -> None:
        if (
            len(self.code) != 6
            or not self.code.isdigit()
            or type(self.trade_date) is not date
            or self.status not in ("trading", "suspended")
            or type(self.is_st) is not bool
            or self.source != "baostock"
        ):
            raise ValueError("history_trading_fact_invalid")

    def suspended_cell(self) -> BaoStockDailyCell:
        if self.status != "suspended" or self.is_st:
            raise ValueError("history_suspension_fact_required")
        raw = BaoStockDailySide(
            self.code,
            self.trade_date,
            "unadjusted",
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
        qfq = BaoStockDailySide(
            self.code,
            self.trade_date,
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
        return BaoStockDailyCell(self.code, self.trade_date, "supplier_marked_suspended", raw, qfq)


__all__ = ["HistoryTradingFact"]
