from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.recommendation.runtime.long_runtime import _Quotes, _runtime
from tests.helpers.recommendation.market.market_data_merge import _observation
from tests.helpers.recommendation.configuration import HardFilterPolicyFixture
from trader.infra.shutdown import ShutdownDeadline
from trader.recommendation.application.ports.long import LongRefreshRequest
from trader.recommendation.application.pipeline.dynamic_market.market_snapshot_service import DynamicSourceHealth
from trader.recommendation.domain.candidate.filters import LevelOneFilterRules
from trader.recommendation.domain.market.quote_freshness import QuoteFreshnessAgeSeconds, QuoteSessionTimes, QuoteSnapshotTime
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.normalization.merge import MergeMarketObservations, MergeObservationLimits

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_CLOSE = datetime(2026, 10, 22, 15, tzinfo=_SHANGHAI)


@pytest.mark.parametrize("hour,minute", [(15, 0), (15, 10), (22, 50), (23, 59)])
def test_same_day_close_survives_merge_and_scoring_filters(application_feature_factory, hour, minute):
    now = _CLOSE.replace(hour=hour, minute=minute)
    observation = _observation("tencent", source_time=_CLOSE.astimezone(timezone.utc), received_at=now, observed_at=now)
    snapshot = MergeMarketObservations(
        (observation,), observed_at=now, limits=MergeObservationLimits(max_age_seconds=30), merge_epoch="close"
    )
    assert len(snapshot.quotes) == 1
    quote = snapshot.quotes[0]
    assert quote.source_time == _CLOSE
    assert quote.age_seconds(now) == (now - _CLOSE).total_seconds()
    feature = replace(application_feature_factory("600001", now), quote=quote)
    rules = LevelOneFilterRules(max_age_seconds=30, policy=HardFilterPolicyFixture())
    rule = next(rule for rule in rules if rule.name == "invalid_quote_time")
    assert rule.predicate(feature, now) is None
    health = DynamicSourceHealth((feature,), now)
    assert health.healthy_source_count == 1
    assert health.age_seconds == quote.age_seconds(now)


@pytest.mark.parametrize(
    "source,observed",
    [
        (_CLOSE - timedelta(minutes=1), _CLOSE.replace(hour=22)),
        (_CLOSE, _CLOSE + timedelta(days=1)),
        (_CLOSE + timedelta(days=1), _CLOSE),
        (_CLOSE.replace(hour=10), _CLOSE.replace(hour=10, minute=1)),
        (_CLOSE.replace(hour=11, minute=20), _CLOSE.replace(hour=12)),
    ],
)
def test_old_intraday_previous_day_and_future_quotes_remain_ineligible(source, observed):
    assert QuoteFreshnessAgeSeconds(source, observed) > 30
    snapshot = MergeMarketObservations(
        (_observation("tencent", source_time=source, received_at=observed, observed_at=observed),),
        observed_at=observed,
        limits=MergeObservationLimits(max_age_seconds=30),
        merge_epoch="invalid-close",
    )
    assert snapshot.quotes == ()


def test_intraday_fresh_quote_keeps_normal_age():
    source = _CLOSE.replace(hour=10)
    assert QuoteFreshnessAgeSeconds(source, source + timedelta(seconds=12)) == 12


def test_configured_early_stop_cannot_replace_formal_close_with_an_intraday_quote():
    sessions = QuoteSessionTimes(600, 730, 810, 885)
    early = _CLOSE.replace(hour=14, minute=45)
    assert QuoteSnapshotTime(early, sessions=sessions) == early
    assert QuoteFreshnessAgeSeconds(early, early.replace(minute=55), sessions=sessions) == 0
    assert QuoteSnapshotTime(_CLOSE.replace(hour=22), sessions=sessions) == _CLOSE
    assert QuoteFreshnessAgeSeconds(early, _CLOSE.replace(hour=22), sessions=sessions) > 30
    assert QuoteFreshnessAgeSeconds(_CLOSE, _CLOSE.replace(hour=22), sessions=sessions) == 0


@pytest.mark.parametrize(
    "clock,anchor",
    [("09:29:59", None), ("09:30:00", None), ("11:29:59", None), ("11:30:00", "11:30:00"),
     ("12:59:59", "11:30:00"), ("13:00:00", None), ("14:59:59", None), ("15:00:00", "15:00:00"),
     ("23:59:59", "15:00:00")],
)
def test_fixed_quote_anchor_uses_shanghai_session_boundaries(clock, anchor):
    observed = datetime.fromisoformat(f"2026-10-22T{clock}+08:00")
    expected = None if anchor is None else datetime.fromisoformat(f"2026-10-22T{anchor}+08:00")
    assert QuoteSnapshotTime(observed.astimezone(timezone.utc)) == expected


@pytest.mark.parametrize("hour,minute", [(11, 30), (12, 0), (12, 59), (13, 0)])
def test_lunch_quote_survives_merge_and_scoring_without_changing_source_time(
    application_feature_factory, hour, minute,
):
    source = _CLOSE.replace(hour=11, minute=30)
    observed = source.replace(hour=hour, minute=minute)
    snapshot = MergeMarketObservations(
        (_observation("tencent", source_time=source.astimezone(timezone.utc), received_at=observed, observed_at=observed),),
        observed_at=observed,
        limits=MergeObservationLimits(max_age_seconds=30),
        merge_epoch="lunch",
    )
    assert len(snapshot.quotes) == 1
    quote = snapshot.quotes[0]
    assert quote.source_time == source
    assert quote.age_seconds(observed) == (observed - source).total_seconds()
    feature = replace(application_feature_factory("600001", observed), quote=quote)
    rule = next(rule for rule in LevelOneFilterRules(
        max_age_seconds=30, policy=HardFilterPolicyFixture(),
    ) if rule.name == "invalid_quote_time")
    assert rule.predicate(feature, observed) is None
    health = DynamicSourceHealth((feature,), observed)
    assert health.healthy_source_count == 1
    assert health.age_seconds == quote.age_seconds(observed)


@pytest.mark.parametrize(
    "source_clock,observed_clock,expected_age",
    [
        ("11:29:29", "12:30:00", 31),
        ("11:29:30", "12:30:00", 30),
        ("11:29:45", "13:00:15", 30),
        ("11:29:45", "13:00:16", 31),
        ("11:30:00", "13:00:30", 30),
        ("11:30:00", "13:00:31", 31),
        ("13:00:00", "13:00:31", 31),
    ],
)
def test_lunch_excludes_only_closed_market_time(source_clock, observed_clock, expected_age):
    source = datetime.fromisoformat(f"2026-10-22T{source_clock}+08:00")
    observed = datetime.fromisoformat(f"2026-10-22T{observed_clock}+08:00")
    assert QuoteFreshnessAgeSeconds(source.astimezone(timezone.utc), observed) == expected_age
    snapshot = MergeMarketObservations(
        (_observation("tencent", source_time=source, received_at=observed, observed_at=observed),),
        observed_at=observed,
        limits=MergeObservationLimits(max_age_seconds=30),
        merge_epoch="lunch-boundary",
    )
    assert bool(snapshot.quotes) is (expected_age <= 30)


@pytest.mark.parametrize("source_hour,source_minute,observed_hour", [(11, 30, 12), (15, 0, 22)])
def test_fixed_session_long_displays_merged_quote_without_formal_record(
    application_feature_factory, source_hour, source_minute, observed_hour,
):
    source = _CLOSE.replace(hour=source_hour, minute=source_minute)
    now = _CLOSE.replace(hour=observed_hour)
    snapshot = MergeMarketObservations(
        (_observation("tencent", source_time=source, received_at=now, observed_at=now),),
        observed_at=now,
        limits=MergeObservationLimits(max_age_seconds=30),
        merge_epoch="long-close",
    )
    features = tuple(replace(application_feature_factory(q.code, now), quote=q) for q in snapshot.quotes)
    runtime, index = _runtime(_Quotes([features]), completed_at=now)
    runtime.start()
    try:
        assert runtime.offer_refresh(LongRefreshRequest(now, "after_close"))
        assert runtime.wait_idle(2)
        current = index.snapshot(Strategy.LONG).current
        assert current is not None
        assert current.items[0].price == 10
        assert current.items[0].source_time == source
        assert current.items[0].quote_status == "live"
        assert "long_quotes_stale" not in runtime.status().degraded_reasons
        assert index.snapshot(Strategy.LONG).formal is None
    finally:
        runtime.stop(wait=True, deadline=ShutdownDeadline.start(2))
