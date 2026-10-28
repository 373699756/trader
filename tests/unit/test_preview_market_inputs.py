"""Preview keeps the signal day independent from the actual collection day."""

from concurrent.futures import Future
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import pytest

from tests.component.market_data_test_support import (
    AFTERNOON,
    FEATURE_WEIGHT_POLICY,
    LONG_POLICY,
    MARKET_REGIME_POLICY,
    NEWS_POLICY,
    TAIL_POLICY,
    BoundedExecutor,
    FakeSession,
    FeatureBuilder,
    MutableMonotonic,
    SourceLaneScheduler,
    StaticMarketClient,
    StaticTencentClient,
    _service,
    _tail_minute_bars,
)
from tests.component.test_candidate_history_tail import CALENDAR, NOW, HistoryInput
from tests.unit.application.scoring_helpers import profile_for
from tests.unit.application.test_tomorrow_production_model import _model_feature
from tests.unit.application.test_v5_model_filtering import _SequencePredictor
from tests.unit.domain.test_tail import POLICY
from tests.unit.infra.test_shared_history_projection import _cache
from tests.unit.test_market_data_merge import _observation
from trader.recommendation.application.pipeline.dynamic_market.market_snapshot_service import DynamicSourceHealth
from trader.recommendation.application.pipeline.dynamic_standardize.dynamic_normalization import (
    NormalizeCandidateDiscoveryPopulation,
)
from trader.recommendation.application.pipeline.local_score.model_scoring import ProductionModelScoringService
from trader.recommendation.application.runtime.source_lanes import SourceRequestSupersededError
from trader.recommendation.domain.candidate.filters import LevelOneFilterRules
from trader.recommendation.domain.market.quote_freshness import QuoteFreshnessAgeSeconds
from trader.recommendation.domain.market.tail import DeriveTailSignals
from trader.recommendation.domain.market.v5.features import ModelPriceVolumeSequence
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.publication.preview import PreviewContext
from trader.recommendation.domain.selection.execution_cost import ExecutionCostPolicy
from trader.recommendation.infra.market_data.gateway import MarketDataGateway
from trader.recommendation.infra.market_data.intraday_loader import IntradayLoader
from trader.recommendation.infra.market_data.market_task_runner import MarketTaskRunner
from trader.recommendation.infra.market_data.providers.eastmoney import EastmoneyClient
from trader.recommendation.infra.normalization.merge import MergeMarketObservations, MergeObservationLimits


def test_preview_accepts_only_its_close_without_rewriting_quote_age(application_feature_factory):
    close = NOW.replace(hour=15)
    observed = NOW + timedelta(days=2)
    context = PreviewContext("market-preview", close.date(), observed)
    observation = _observation("tencent", source_time=close, received_at=observed, observed_at=observed)
    snapshot = MergeMarketObservations(
        (observation,),
        observed_at=observed,
        merge_epoch="preview",
        limits=MergeObservationLimits(max_age_seconds=30, preview_context=context),
    )
    assert len(snapshot.quotes) == 1
    quote = snapshot.quotes[0]
    assert quote.source_time == close and quote.received_time == observed
    assert snapshot.source_ages_seconds["tencent"] == (observed - close).total_seconds()
    feature = replace(application_feature_factory(quote.code, observed), quote=quote, preview_context=context)
    rule = next(rule for rule in LevelOneFilterRules(max_age_seconds=30) if rule.name == "invalid_quote_time")
    assert rule.predicate(feature, observed) is None
    assert NormalizeCandidateDiscoveryPopulation((feature,), observed)[0].quote.source_time == close
    health = DynamicSourceHealth((feature,), observed)
    assert health.healthy_source_count == 1
    assert health.age_seconds == (observed - close).total_seconds()
    assert rule.predicate(replace(feature, preview_context=None), observed).code == "stale_quote"
    for invalid in (close - timedelta(minutes=1), close - timedelta(days=1), close + timedelta(days=1)):
        assert QuoteFreshnessAgeSeconds(invalid, observed, preview_context=context) == float("inf")


def test_preview_tail_uses_market_day_and_actual_receipt_cutoff():
    bars = _tail_minute_bars()
    observed = AFTERNOON + timedelta(days=2)
    context = PreviewContext("minute-preview", AFTERNOON.date(), observed)
    delivered = tuple(replace(bar, received_time=observed) for bar in bars)
    expected = DeriveTailSignals(bars, observed_at=AFTERNOON, policy=POLICY)
    signals = DeriveTailSignals(delivered, observed_at=observed, policy=POLICY, preview_context=context)
    assert signals.return_pct == expected.return_pct
    assert signals.volume_ratio == expected.volume_ratio
    assert signals.latest_at == bars[-1].source_time
    assert signals.received_at == observed
    assert DeriveTailSignals(delivered, observed_at=observed, policy=POLICY).valid_bar_count == 0
    future = tuple(replace(bar, received_time=observed + timedelta(seconds=1)) for bar in delivered)
    assert DeriveTailSignals(future, observed_at=observed, policy=POLICY, preview_context=context).valid_bar_count == 0


def test_preview_compares_close_quotes_collected_at_different_real_times():
    observed = NOW + timedelta(days=2)
    context = PreviewContext("close-comparison", NOW.date(), observed)
    close = context.market_as_of
    full = _observation("tencent_market", source_time=close, received_at=observed, observed_at=observed)
    received = observed + timedelta(seconds=2)
    targeted = _observation("tencent", source_time=close, received_at=received, observed_at=received)
    old = _observation("sina", source_time=close - timedelta(days=1), received_at=observed, observed_at=observed)
    snapshot = MergeMarketObservations(
        (full, targeted, old),
        observed_at=received,
        merge_epoch="preview-targeted",
        limits=MergeObservationLimits(targeted_codes=("600001",), max_age_seconds=30, preview_context=context),
    )
    assert len(snapshot.quotes) == 1
    assert snapshot.quotes[0].received_time == received
    assert snapshot.quotes[0].source_time == close
    assert not any("conflict" in reason for reason in snapshot.degraded_reasons)
    assert full.observed_at == observed and targeted.observed_at == received


@pytest.mark.parametrize("source_offset", (timedelta(), -timedelta(minutes=1), -timedelta(days=1)))
def test_long_service_reads_close_without_preparing_short_model_preview(application_feature_factory, source_offset):
    observed = NOW + timedelta(days=2)
    context = PreviewContext("long-service-preview", NOW.date(), observed)
    quote = replace(
        application_feature_factory("600001", observed).quote,
        source_time=context.market_as_of + source_offset,
        received_time=observed,
        pct_change=-1.25,
    )
    gateway = MarketDataGateway(
        StaticMarketClient((quote,)),
        StaticMarketClient((quote,)),
        StaticTencentClient((quote,)),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        wall_clock=lambda: observed,
    )
    service = _service(
        gateway,
        None,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        wall_clock=lambda: observed,
    )
    features = service.refresh_long_quotes((quote.code,), observed, preview_context=context)
    if source_offset == timedelta():
        assert len(features) == 1
        assert features[0].quote.source_time == context.market_as_of
        assert features[0].quote.received_time == observed
        assert features[0].quote.pct_change == -1.25
    else:
        assert features == ()
    # Long carries its own context; the shared short-strategy mode stays unchanged.
    assert gateway._preview_context is None


def test_service_prepares_shared_preview_collection_and_feature_builder(application_feature_factory):
    observed = NOW + timedelta(days=2)
    context = PreviewContext("service-preview", NOW.date(), observed)
    quote = replace(
        application_feature_factory("600001", observed).quote, source_time=context.market_as_of, received_time=observed
    )
    gateway = MarketDataGateway(
        StaticMarketClient((quote,)),
        StaticMarketClient((quote,)),
        StaticTencentClient((quote,)),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        wall_clock=lambda: observed,
    )
    source = HistoryInput(252, 0, (quote.code,))
    source.current = replace(
        source.current, data_cutoff=NOW.date(), calendar_dates=tuple(day for day in CALENDAR if day <= NOW.date())
    )
    history = _cache(source, 251)
    history.initialize(cancelled=lambda: False)
    service = _service(
        gateway,
        None,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        published_history=history,
        wall_clock=lambda: observed,
    )
    service.prepare_preview(context)
    fetched = gateway.fetch_market(observed_at=observed)
    assert len(fetched) == 1 and fetched[0].source_time == context.market_as_of
    features = service.quotes.build_market_features(
        fetched, history.cached((quote.code,), observed_at=observed), observed, action_restrictions={}
    )
    assert features[0].preview_context is context
    assert features[0].history_days == 251 and features[0].model_history_quality.failure is None
    assert features[0].observed_at == observed
    anchor = history.entries()[quote.code].context.anchor_price(1, NOW.date().isoformat())
    assert features[0].optional_value("qfq_return_1d") == pytest.approx(quote.price / anchor - 1.0)
    service.prepare_preview(None)
    assert gateway.fetch_market(observed_at=observed) == ()


def test_preview_minute_provider_preserves_receipt_time_and_excludes_other_sessions():
    observed = AFTERNOON + timedelta(days=2)
    context = PreviewContext("provider-preview", AFTERNOON.date(), observed)
    session = FakeSession(
        [
            {
                "data": {
                    "trends": [
                        "2026-07-16 15:00,10,10.1,10.2,9.9,100,1010,10",
                        "2026-07-15 15:00,10,10.1,10.2,9.9,100,1010,10",
                        "2026-07-16 15:01,10,10.1,10.2,9.9,100,1010,10",
                    ]
                }
            }
        ]
    )
    client = EastmoneyClient(
        worker_pool=None,
        timeout_seconds=2,
        session_factory=lambda: session,
        wall_clock=lambda: observed,
        monotonic=lambda: 1.0,
    )
    bars = client.fetch_intraday_minutes("600001", now=observed, preview_context=context)
    assert len(bars) == 1
    assert bars[0].source_time == context.market_as_of
    assert bars[0].received_time == observed


def test_preview_minute_lane_preserves_real_failure_and_retained_close_qualification():
    observed = AFTERNOON + timedelta(days=2)
    context = PreviewContext("minute-lane-preview", AFTERNOON.date(), observed)
    bars = tuple(replace(bar, received_time=observed) for bar in _tail_minute_bars())
    clock = MutableMonotonic()

    class Minutes:
        calls = 0

        def fetch_intraday_minutes(self, _code, *, now, deadline, cancelled, preview_context):
            assert preview_context is context and now == observed and deadline > clock()
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("preview_minute_network_error")
            return bars

    pool = BoundedExecutor(worker_count=2, queue_capacity=4, thread_name_prefix="preview-minute-lane")
    lanes = SourceLaneScheduler(pool)
    runner = MarketTaskRunner(
        worker_pool=pool,
        source_lanes=lanes,
        cache=None,
        source_contracts={},
        config_version="preview-config",
        schema_version="preview-schema",
        wall_clock=lambda: observed,
    )
    loader = IntradayLoader(
        Minutes(),
        runner,
        workers=1,
        minute_pool=pool,
        ttl_seconds=1,
        batch_timeout_seconds=3,
        capacity=10,
        monotonic=clock,
    )
    loader.prepare_preview(context)
    pool.start()
    try:
        assert loader.load(("600001",), observed)["600001"] == bars
        superseded = Future()
        superseded.set_exception(SourceRequestSupersededError("replaced by another profile"))
        with patch.object(lanes, "submit", return_value=superseded):
            restrictions = {}
            retained = loader.load(("600001", "600002"), observed, action_restrictions=restrictions)
        assert retained == {"600001": bars}
        assert restrictions == {"600001": {"intraday_data_degraded"}}
        assert loader.status().last_error == "intraday_request_superseded"
        clock.value = 2
        restrictions = {}
        assert loader.load(("600001",), observed, action_restrictions=restrictions)["600001"] == bars
        assert restrictions["600001"] == {"intraday_data_degraded"}
        retained_restrictions = {}
        assert (
            loader.cached(("600001",), observed_at=observed, action_restrictions=retained_restrictions)["600001"]
            == bars
        )
        assert retained_restrictions == restrictions
        assert loader.status().last_error == "preview_minute_network_error"
        loader.prepare_preview(None)
        assert loader.cached(("600001",), observed_at=observed) == {}
    finally:
        lanes.stop(wait=True, timeout_seconds=1)
        pool.stop(wait=True, cancel_futures=True)


def test_preview_v5_does_not_admit_a_sequence_including_its_signal_day(application_feature_factory):
    observed = NOW + timedelta(days=2)
    context = PreviewContext("v5-preview", NOW.date(), observed)
    service = ProductionModelScoringService(
        profile_for(_SequencePredictor()), Strategy.TOMORROW, cost_policy=ExecutionCostPolicy(0.002, 1.0)
    )
    base = _model_feature(application_feature_factory("600001", observed), offset=0, amihud=1)
    dates = tuple(NOW.date() - timedelta(days=61 - i) for i in range(61))
    before_signal = ModelPriceVolumeSequence("600001", dates, (0.01,) * 360, "publication")
    feature = replace(base, preview_context=context, model_sequence=before_signal)
    assert service.is_input_eligible(feature)
    including_signal = replace(before_signal, dates=tuple(day + timedelta(days=1) for day in dates))
    assert not service.is_input_eligible(replace(feature, model_sequence=including_signal))
    assert service.is_input_eligible(replace(feature, model_sequence=including_signal, preview_context=None))


@pytest.mark.parametrize(
    "sessions,rank,sequence", ((251, True, False), (251, False, False), (61, False, False), (62, False, True))
)
def test_preview_history_uses_signal_day_for_all_profiles(sessions, rank, sequence):
    source = HistoryInput(252, 0, ("600001",))
    source.current = replace(
        source.current, data_cutoff=NOW.date(), calendar_dates=tuple(day for day in CALENDAR if day <= NOW.date())
    )
    cache = _cache(source, sessions, rank=rank, sequence=sequence)
    cache.initialize(cancelled=lambda: False)
    expected = cache.completed_model_inputs(("600001",), NOW)
    observed = NOW + timedelta(days=2)
    context = PreviewContext("history-preview", NOW.date(), observed)
    cache.prepare_preview(context)
    actual = cache.completed_model_inputs(("600001",), observed)
    assert actual == expected
    assert actual[0].quality.failure is None
    if sequence:
        assert actual[0].sequence.dates[-1] == max(day for day in CALENDAR if day < NOW.date())
    assert (
        cache.cached(("600001",), fresh_only=True, observed_at=observed)["600001"][-1].trade_date
        == NOW.date().isoformat()
    )
    cache.prepare_preview(None)
    assert cache.completed_model_inputs(("600001",), observed) != actual or not (rank or sequence)
