from dataclasses import replace
from datetime import timedelta

import pytest

from tests.component.market_data_test_support import (
    FEATURE_WEIGHT_POLICY,
    LONG_POLICY,
    MARKET_REGIME_POLICY,
    NEWS_POLICY,
    NOW,
    TAIL_POLICY,
    CountingHistoryClient,
    FeatureBuilder,
    StaticGateway,
    _history_bars,
    _quote,
    _service,
)
from trader.recommendation.domain.evidence.pipeline import PIPELINE_STAGES, StageState
from trader.recommendation.domain.market.static import StaticIssuer, StaticMarketReference
from trader.recommendation.infra.market_data.official_static_reference import StaticReferenceRead
from trader.recommendation.infra.market_data.static_market_cache import StaticMarketCache


def test_static_baseline_reuses_identity_and_source_age_across_dynamic_changes() -> None:
    cache = StaticMarketCache()
    quote = _quote()
    issuer = StaticIssuer(quote.code, quote.name, quote.board, quote.exchange, quote.listing_date, False, False)
    reference = StaticMarketReference((issuer,), "official-a", NOW)
    source = StaticReferenceRead(reference, "reference-a", False, 86400)
    first = cache.read(source, NOW)
    second = cache.read(source, NOW + timedelta(seconds=20))

    assert second.cache_hit
    assert second.baseline is first.baseline
    assert second.baseline.health(NOW + timedelta(seconds=20)).age_seconds == 20
    assert first.baseline.records[0].name == quote.name
    changed_reference = cache.read(replace(source, reference_epoch="reference-b"), NOW)
    assert not changed_reference.cache_hit
    assert changed_reference.baseline.identity != first.baseline.identity
    changed_fact = cache.read(
        replace(source, reference=replace(reference, records=(replace(issuer, name="new name"),))), NOW
    )
    assert not changed_fact.cache_hit
    assert changed_fact.baseline.records[0].name == "new name"
    assert first.baseline.records[0].name == quote.name


def test_real_static_stages_leave_missing_identity_pending_before_history() -> None:
    class AdvancingClock:
        value = 0.0

        def __call__(self) -> float:
            self.value += 0.01
            return self.value

    class RecordingGateway(StaticGateway):
        calls = 0
        fail = False

        def fetch_market(self, **kwargs):
            self.calls += 1
            if self.fail:
                raise RuntimeError("fixture supplier failure")
            return super().fetch_market(**kwargs)

    gateway = RecordingGateway((_quote("600001"), replace(_quote("600002"), name=" ")))
    history = CountingHistoryClient(_history_bars())
    service = _service(
        gateway,
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        monotonic=AdvancingClock(),
    )
    first = service.fetch_market_feature_batch(NOW)

    assert tuple(stage.stage for stage in first.static_stages) == PIPELINE_STAGES[:4]
    assert tuple((stage.input_count, stage.output_count) for stage in first.static_stages) == (
        (2, 2),
        (2, 2),
        (2, 1),
        (1, 1),
    )
    normalized = first.static_stages[2]
    assert normalized.pending_count == 1
    assert normalized.rejected_count == 0
    assert normalized.state is StageState.DEGRADED
    assert [(reason.code, reason.count) for reason in normalized.reasons] == [("static_identity_pending", 1)]
    assert all(stage.latency_ms > 0 for stage in first.static_stages)
    assert tuple(feature.quote.code for feature in first.features) == ("600001",)
    assert first.dynamic_stage.records == first.features
    assert first.dynamic_stage.snapshot.input_batch_id == first.static_stages[-1].output_batch_id
    assert first.dynamic_stage.snapshot.input_count == first.static_stages[-1].output_count
    assert first.dynamic_stage.snapshot.latency_ms > 0
    assert first.dynamic_stage.snapshot.source_health.latest_success_at == NOW
    assert history.calls == ["600001"]
    assert all(
        left.output_count == right.input_count and left.output_batch_id == right.input_batch_id
        for left, right in zip(first.static_stages, first.static_stages[1:], strict=False)
    )

    cached = service.fetch_market_feature_batch(NOW + timedelta(seconds=1))
    assert gateway.calls == 1
    assert cached.static_stages[1].reasons[0].code == "static_baseline_reused"
    assert cached.static_stages[2].pending_count == 1
    assert cached.static_stages[0].source_health.age_seconds == 1
    assert first.static_stages[0].source_health.age_seconds == 0
    assert cached.dynamic_stage.snapshot.source_health.age_seconds == 1
    assert cached.dynamic_stage.snapshot.source_health.latest_success_at == NOW
    assert cached.dynamic_stage.snapshot.output_batch_id != first.dynamic_stage.snapshot.output_batch_id
    gateway._quotes = tuple(
        replace(quote, price=quote.price + 1, received_time=NOW + timedelta(seconds=10)) for quote in gateway._quotes
    )
    refreshed = service.fetch_market_feature_batch(NOW + timedelta(seconds=10), force=True)
    assert refreshed.static_stages[1].reasons[0].code == "static_baseline_reused"
    assert refreshed.static_stages[0].source_health.age_seconds == 10
    assert refreshed.features[0].quote.price == first.features[0].quote.price + 1
    assert refreshed.dynamic_stage.snapshot.source_health.latest_success_at == NOW + timedelta(seconds=10)
    assert first.features[0].quote.price == _quote().price
    assert gateway.calls == 2
    gateway.fail = True
    with pytest.raises(RuntimeError, match="fixture supplier failure"):
        service.fetch_market_feature_batch(NOW, force=True)
    retained = service.fetch_market_feature_batch(NOW + timedelta(seconds=11))
    assert retained.features == refreshed.features
    assert retained.static_stages[2].pending_count == 1
    assert gateway.calls == 3


def test_empty_static_population_is_not_reported_as_completed() -> None:
    service = _service(
        StaticGateway(()),
        CountingHistoryClient(()),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
    )
    batch = service.fetch_market_feature_batch(NOW)
    assert all(stage.state is StageState.NOT_READY for stage in batch.static_stages)
    assert all(stage.source_health.state.value == "unavailable" for stage in batch.static_stages)
