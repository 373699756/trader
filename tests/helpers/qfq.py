"""Immutable price windows for isolated qfq persistence tests."""

from dataclasses import replace
from datetime import date, timedelta

from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockDailySide
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryWindow


def qfq_window(code: str) -> PublishedHistoryWindow:
    cells = []
    for offset in range(252):
        day = date(2025, 1, 1) + timedelta(days=offset)
        raw = BaoStockDailySide(
            code,
            day,
            "unadjusted",
            10.0,
            10.2,
            9.8,
            10.0,
            100000.0,
            10000000.0,
            9.99,
            0.1,
            1.0,
            "trading",
        )
        qfq = replace(raw, adjustment="qfq", preclose=None, pct_change=None, turnover=None)
        cells.append(ProjectHistoryCell(BaoStockDailyCell(code, day, "complete", raw, qfq)))
    return PublishedHistoryWindow(code, tuple(cells))
