from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tests.unit.application.test_long_runtime import _Quotes, _runtime
from tests.unit.test_market_data_merge import _observation
from trader.infra.shutdown import ShutdownDeadline
from trader.recommendation.application.ports.long import LongRefreshRequest
from trader.recommendation.application.pipeline.dynamic_market.market_snapshot_service import DynamicSourceHealth
from trader.recommendation.domain.candidate.filters import LevelOneFilterRules
from trader.recommendation.domain.market.quote_freshness import QuoteFreshnessAgeSeconds
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
    rule = next(rule for rule in LevelOneFilterRules(max_age_seconds=30) if rule.name == "invalid_quote_time")
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


def test_evening_long_displays_merged_close_without_formal_record(application_feature_factory):
    now = _CLOSE.replace(hour=22)
    snapshot = MergeMarketObservations(
        (_observation("tencent", source_time=_CLOSE, received_at=now, observed_at=now),),
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
        assert current.items[0].source_time == _CLOSE
        assert index.snapshot(Strategy.LONG).formal is None
    finally:
        runtime.stop(wait=True, deadline=ShutdownDeadline.start(2))
