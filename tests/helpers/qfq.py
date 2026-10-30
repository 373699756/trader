"""Immutable price windows for isolated qfq persistence tests."""

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockDailySide
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryWindow
from trader.download.infra.download_settings import LoadQfqWindowConfiguration
from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.download.infra.tencent_qfq_supplier import TencentQfqOptions

QFQ_CONFIGURATION = LoadQfqWindowConfiguration(Path(__file__).resolve().parents[2])
QFQ_TENCENT_OPTIONS = TencentQfqOptions(
    timeout_seconds=QFQ_CONFIGURATION.source_timeout_seconds,
    retries=QFQ_CONFIGURATION.source_retries,
)


def publish_windows(cache: SQLiteQfqWindowCache, windows: tuple[PublishedHistoryWindow, ...], source: str):
    calendar = tuple(sorted({cell.trade_date for window in windows for cell in window.cells}))[-251:]
    clipped = tuple(
        replace(window, cells=tuple(cell for cell in window.cells if cell.trade_date >= calendar[0]))
        for window in windows
    )
    peer = SQLiteQfqWindowCache(cache.path, "v3" if cache.profile == "v2" else "v2")
    return cache.replace_paired_windows(
        peer, clipped, source, QFQ_CONFIGURATION, calendar=calendar, completed_day=None
    )


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
