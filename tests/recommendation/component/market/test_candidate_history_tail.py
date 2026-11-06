from __future__ import annotations

import time

from dataclasses import replace

from datetime import date, datetime, timedelta


import pytest

from tests.helpers.recommendation.market_data import (
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


from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryWindow

from trader.recommendation.infra.market_data.published_history_bars import QfqBars

from trader.infra.settings.models import HistoryPreloadSettings

from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache

from trader.recommendation.infra.market_data.shared_history_projection import SharedHistoryProjection

from tests.helpers.recommendation.market.candidate_history_tail import (
    CALENDAR,
    HistoryInput,
    NOW,
    _cache,
    _cell,
)


def test_qfq_projection_uses_actual_raw_volume_with_adjusted_prices() -> None:
    cell = _cell("600001", NOW.date())
    assert cell.qfq is not None and cell.unadjusted is not None
    adjusted = replace(cell.qfq, close_price=5.0, volume=200_000.0)
    published = ProjectHistoryCell(replace(cell, qfq=adjusted))
    bars = QfqBars(PublishedHistoryWindow("600001", (published,)))
    assert bars[0].close == 5.0
    assert bars[0].volume == cell.unadjusted.volume == 100_000.0
    assert bars[0].turnover_rate == cell.unadjusted.turnover == 1.0


@pytest.mark.parametrize("sessions", (61, 251))
@pytest.mark.parametrize("gap", (0, 1, 3, 6))
def test_candidate_history_never_reads_disk_after_preload(sessions, gap, monkeypatch):
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
    history = PublishedHistoryCache(
        ReadPublishedHistoryUseCase(published), lookback_sessions=61,
        open_dates=lambda: CALENDAR, monotonic=time.monotonic,
        projection_owner=SharedHistoryProjection(
            ReadPublishedHistoryUseCase(published), monotonic=time.monotonic,
            settings=HistoryPreloadSettings(warn_after_seconds=300.0),
        ),
    )
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
