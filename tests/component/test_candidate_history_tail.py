from __future__ import annotations

import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.component.market_data_test_support import (
    FEATURE_WEIGHT_POLICY,
    LONG_POLICY,
    MARKET_REGIME_POLICY,
    NEWS_POLICY,
    TAIL_POLICY,
    FeatureBuilder,
    StaticGateway,
    StaticHistoryClient,
    _quote,
    _service,
)
from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockDailySide
from trader.download.domain.published_history import (
    ProjectHistoryCell,
    PublishedHistoryManifest,
    PublishedHistoryWindow,
)
from trader.recommendation.infra.market_data.published_history_bars import QfqBars
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache

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


def test_qfq_projection_uses_actual_raw_volume_with_adjusted_prices() -> None:
    cell = _cell("600001", NOW.date())
    assert cell.qfq is not None and cell.unadjusted is not None
    adjusted = replace(cell.qfq, close_price=5.0, volume=200_000.0)
    published = ProjectHistoryCell(replace(cell, qfq=adjusted))
    bars = QfqBars(PublishedHistoryWindow("600001", (published,)))
    assert bars[0].close == 5.0
    assert bars[0].volume == cell.unadjusted.volume == 100_000.0
    assert bars[0].turnover_rate == cell.unadjusted.turnover == 1.0


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

    def iter_windows(self, manifest, *, sessions, check=None):
        for code in manifest.universe_codes:
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
    )
    history.initialize(cancelled=lambda: False)
    return history, published


@pytest.mark.parametrize("sessions", (61, 251))
@pytest.mark.parametrize("gap", (0, 1, 3, 6))
def test_candidate_history_never_reads_storage_after_preload(sessions, gap, monkeypatch):
    history, published = _cache(sessions, gap)
    before = history.entries()

    def unexpected(*args, **kwargs):
        pytest.fail("candidate history must only consume the startup projection")

    for name in ("manifest", "iter_windows", "read_windows", "read_range"):
        monkeypatch.setattr(published, name, unexpected, raising=False)
    for _ in range(2):
        restrictions = {}
        loaded = history.load(("600001", "600002"), observed_at=NOW, action_restrictions=restrictions)
        assert set(loaded) == ({"600001"} if gap == 0 else set())
        assert restrictions == {
            code: {"history_data_pending"} for code in (("600002",) if gap == 0 else ("600001", "600002"))
        }
        assert history.cached(("600001",), observed_at=NOW, fresh_only=True) == loaded
    assert history.entries() == before


def test_new_day_requires_manual_sync_and_restart(monkeypatch):
    history, published = _cache()
    published.current = replace(published.current, snapshot_hash="b" * 64)
    assert history.load(("600001",), observed_at=NOW)
    assert history.load(("600001",), observed_at=NOW + timedelta(days=1)) == {}
    assert history.loaded_identity == "a" * 64


@pytest.mark.parametrize("observed_on,cutoff", ((date(2026, 5, 5), date(2026, 4, 30)),
                                              (date(2026, 7, 19), date(2026, 7, 17))))
def test_holiday_and_weekend_accept_last_completed_session(observed_on, cutoff):
    published = HistoryInput(61, 0, ("600001",))
    published.current = replace(published.current, data_cutoff=cutoff,
                                calendar_dates=tuple(day for day in CALENDAR if day <= cutoff))
    history = PublishedHistoryCache(ReadPublishedHistoryUseCase(published), lookback_sessions=61,
                                    open_dates=lambda: CALENDAR, monotonic=time.monotonic)
    history.initialize(cancelled=lambda: False)
    assert history.load(("600001",), observed_at=datetime.combine(observed_on, NOW.timetz()))


def test_candidate_features_keep_missing_history_restricted():
    history, _ = _cache(gap=3)
    service = _service(
        StaticGateway((replace(_quote("600001"), source_time=NOW),)), StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        published_history=history, wall_clock=lambda: NOW,
    )
    features = service.refresh_candidate_quotes(("600001",), NOW)
    assert features[0].history_days == 0
    assert "history_data_pending" in features[0].quote.execution_restrictions
    assert service.read_candidate_features(("600001",), NOW)[0].history_days == 0
