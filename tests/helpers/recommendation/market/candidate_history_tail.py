from __future__ import annotations
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockDailySide
from trader.download.domain.published_history import (
    ProjectHistoryCell,
    PublishedHistoryManifest,
    PublishedHistoryWindow,
)
from trader.infra.settings.models import HistoryPreloadSettings
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache
from trader.recommendation.infra.market_data.shared_history_projection import SharedHistoryProjection

NOW = datetime(2026, 7, 16, 10, tzinfo=ZoneInfo("Asia/Shanghai"))

CALENDAR = tuple(
    day
    for offset in range(600)
    if (day := date(2025, 1, 1) + timedelta(days=offset)).weekday() < 5
    and day not in {date(2026, 5, 1), date(2026, 5, 4), date(2026, 5, 5)}
)


def _cell(code: str, day: date) -> BaoStockDailyCell:
    price = 10.0 + (day - date(2025, 1, 1)).days / 1000
    raw = BaoStockDailySide(
        code,
        day,
        "unadjusted",
        price,
        price + 0.2,
        price - 0.2,
        price,
        100000.0,
        10000000.0 + price,
        price - 0.01,
        0.1,
        1.0,
        "trading",
    )
    qfq = replace(raw, adjustment="qfq", preclose=None, pct_change=None, turnover=None)
    return BaoStockDailyCell(code, day, "complete", raw, qfq)


class HistoryInput:
    def __init__(self, sessions: int, gap: int, codes: tuple[str, ...]) -> None:
        dates = tuple(day for day in CALENDAR if day < NOW.date())
        cutoff = dates[-gap - 1] if gap else dates[-1]
        self.current = PublishedHistoryManifest(
            "a" * 64, 1, cutoff, tuple(day for day in CALENDAR if day <= cutoff), codes
        )
        self.sessions = sessions
        self.reads: list[tuple[str, ...]] = []

    def manifest(self):
        return self.current

    def iter_windows(self, manifest, *, sessions, codes=None, check=None):
        for code in manifest.universe_codes:
            if codes is not None and code not in codes:
                continue
            if check is not None:
                check()
            yield self.window(code, manifest.calendar_dates[-sessions:])

    def read_windows(self, manifest, codes, *, sessions, check=None):
        if check is not None:
            check()
        self.reads.append(tuple(codes))
        return tuple(self.window(code, manifest.calendar_dates[-sessions:]) for code in codes)

    def read_range(self, manifest, codes, start, end, *, check=None):
        if check is not None:
            check()
        self.reads.append(tuple(codes))
        dates = tuple(day for day in manifest.calendar_dates if start <= day <= end)
        return tuple(self.window(code, dates) for code in codes)

    @staticmethod
    def window(code: str, dates: tuple[date, ...]) -> PublishedHistoryWindow:
        return PublishedHistoryWindow(code, tuple(ProjectHistoryCell(_cell(code, day)) for day in dates))


def _cache(sessions=61, gap=0):
    published = HistoryInput(sessions, gap, ("600001",))
    history = PublishedHistoryCache(
        ReadPublishedHistoryUseCase(published), lookback_sessions=sessions,
        open_dates=lambda: CALENDAR, monotonic=time.monotonic,
        projection_owner=SharedHistoryProjection(
            ReadPublishedHistoryUseCase(published), monotonic=time.monotonic,
            settings=HistoryPreloadSettings(warn_after_seconds=300.0),
        ),
    )
    history.initialize(cancelled=lambda: False)
    return history, published
