from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

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
    MarketDataGateway,
    StaticGateway,
    StaticMarketClient,
    StaticTencentClient,
    _history_bars,
    _quote,
    _service,
)
from tests.component.test_candidate_history_tail import _Archive
from tests.reference_fixtures import PreparedFixture
from tests.unit.infra.test_history_supplier_router import _current_universe
from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.recommendation.application.pipeline.static_market import static_pipeline
from trader.recommendation.application.pipeline.static_market.static_market_cache import StaticMarketCache
from trader.recommendation.application.pipeline.static_market.static_pipeline import StaticMarketPipeline
from trader.recommendation.application.ports.market_data import MarketDataPendingError, MarketDataUnavailableError
from trader.recommendation.application.ports.static_reference import StaticReferenceRead
from trader.recommendation.domain.evidence.pipeline import PIPELINE_STAGES, StageState
from trader.recommendation.domain.market.static import StaticIssuer, StaticMarketReference
from trader.recommendation.domain.publication.preview import PreviewContext
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache
from trader.recommendation.infra.market_data.published_input_preloader import PublishedInputPreloader
from trader.recommendation.infra.market_data.published_references import PublishedReferences


def test_startup_binds_published_security_facts_before_preview_quotes_are_merged():
    observed = NOW + timedelta(days=2)
    context = PreviewContext("startup-reference-preview", NOW.date(), observed)
    reference = PreparedFixture(observed)
    references = PublishedReferences(lambda: reference, lambda: reference.identity, lambda _: (), max_age_seconds=86400)
    quote = replace(
        _quote(),
        source_time=context.market_as_of,
        received_time=observed,
        listing_date=None,
        listing_age_sessions=None,
    )
    gateway = MarketDataGateway(
        StaticMarketClient((quote,)),
        StaticMarketClient((quote,)),
        StaticTencentClient((quote,)),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        listing_open_dates=lambda: reference.open_dates,
        wall_clock=lambda: observed,
    )
    history = PublishedHistoryCache(
        ReadPublishedHistoryUseCase(_Archive(62, 0, ("600001",))),
        lookback_sessions=61,
        monotonic=lambda: 0.0,
        require_input_binding=True,
    )
    service = _service(
        gateway,
        None,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        published_references=references,
        published_history=history,
        wall_clock=lambda: observed,
    )
    service.initialize_published_inputs(lambda: False, model_version="fixture", config_version="fixture")
    service.prepare_preview(context)
    merged = gateway.fetch_market(observed_at=observed)
    assert len(merged) == 1
    assert merged[0].listing_date == reference.official_universe[0].listed_on
    assert merged[0].listing_age_sessions > 250
    assert "board_identity_degraded" not in merged[0].execution_restrictions
    assert merged[0].source_time == context.market_as_of


@pytest.mark.parametrize("fail_static", (False, True))
def test_startup_does_not_open_refresh_before_static_eligibility_finishes(fail_static):
    class RecordingGateway(StaticGateway):
        calls = 0

        def fetch_market(self, **kwargs):
            self.calls += 1
            return super().fetch_market(**kwargs)

    history = PublishedHistoryCache(
        ReadPublishedHistoryUseCase(_Archive(62, 0, ("600001",))),
        lookback_sessions=61,
        monotonic=lambda: 0.0,
        require_input_binding=True,
    )
    gateway = RecordingGateway((_quote(),))
    service = _service(
        gateway,
        None,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        published_history=history,
    )
    service._static_pipeline = StaticMarketPipeline(
        service.eligibility, cache=StaticMarketCache(), monotonic=lambda: 0.0
    )
    initialize = service._static_pipeline.initialize
    observed_pending = []

    def initializing(reference, observed_at):
        with pytest.raises(MarketDataPendingError):
            service.fetch_market_feature_batch(NOW)
        observed_pending.append(True)
        assert gateway.calls == 0
        if fail_static:
            raise RuntimeError("static_eligibility_preload_capacity")
        initialize(reference, observed_at)

    preloader = PublishedInputPreloader(
        history,
        initialize=lambda cancelled: service.initialize_published_inputs(
            cancelled, model_version="model:fixture", config_version="config:fixture"
        ),
    )
    with patch.object(service._static_pipeline, "initialize", side_effect=initializing):
        preloader.start()
        preloader._thread.join(timeout=5)
        assert not preloader._thread.is_alive()
    assert observed_pending == [True]
    if fail_static:
        with pytest.raises(MarketDataUnavailableError) as failure:
            service.fetch_market_feature_batch(NOW)
        assert not isinstance(failure.value, MarketDataPendingError)
        assert gateway.calls == 0
    else:
        assert service.fetch_market_feature_batch(NOW).features
        assert gateway.calls == 1


def test_static_eligibility_is_built_once_shared_by_matching_profiles_and_rebuilt_on_restart() -> None:
    cache = StaticMarketCache()
    quote = _quote()
    issuer = StaticIssuer(quote.code, quote.name, quote.board, quote.exchange, quote.listing_date, False, False)
    reference = StaticMarketReference((issuer,), "official-a", NOW)
    source = StaticReferenceRead(reference, "reference-a", False, 86400)
    service = _service(
        StaticGateway((quote,)),
        CountingHistoryClient(_history_bars()),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
    )

    def pipeline(cache, blacklist=frozenset()):
        return StaticMarketPipeline(service.eligibility, cache=cache, monotonic=lambda: 1.0, blacklist_codes=blacklist)

    first, second = pipeline(cache), pipeline(cache)
    with patch.object(
        static_pipeline, "NormalizeStaticIssuer", wraps=static_pipeline.NormalizeStaticIssuer
    ) as normalize:
        with patch.object(
            static_pipeline, "FilterPermanentEligibility", wraps=static_pipeline.FilterPermanentEligibility
        ) as qualify:
            first.initialize(source, NOW)
            second.initialize(source, NOW + timedelta(seconds=1))
            first.initialize(replace(source, reference_epoch="reference-b"), NOW)
            assert normalize.call_count == qualify.call_count == 1
            initial = first.observe(NOW)
            for seconds in (1, 20, 86400):
                reused = second.observe(NOW + timedelta(seconds=seconds))
                assert reused[3].records is initial[3].records
                assert reused[2][0].as_of == NOW
                assert reused[2][0].source_health.age_seconds == seconds
                assert reused[2][0].operation_durations == (("startup_reuse", None),)
            assert normalize.call_count == qualify.call_count == 1
            different = pipeline(cache, frozenset({quote.code}))
            different.initialize(source, NOW)
            assert different.observe(NOW)[1] == frozenset()
            assert second.observe(NOW)[1] == {quote.code}
            restarted = pipeline(StaticMarketCache())
            restarted.initialize(source, NOW + timedelta(days=1))
            assert normalize.call_count == qualify.call_count == 3
            assert restarted.observe(NOW + timedelta(days=1))[2][0].as_of != NOW
            with pytest.raises(RuntimeError, match="static_eligibility_from_future"):
                restarted.observe(NOW)
            rejected = pipeline(StaticMarketCache(reserve=lambda snapshots: False))
            with pytest.raises(RuntimeError, match="static_eligibility_preload_capacity"):
                rejected.initialize(source, NOW)
            with pytest.raises(RuntimeError, match="static_eligibility_not_initialized"):
                rejected.observe(NOW)


def test_matching_profiles_initialize_static_eligibility_concurrently_once() -> None:
    cache = StaticMarketCache()
    quote = _quote()
    source = StaticReferenceRead(
        StaticMarketReference(
            (StaticIssuer(quote.code, quote.name, quote.board, quote.exchange, quote.listing_date, False, False),),
            "official-shared",
            NOW,
        ),
        "reference-shared",
        False,
        86400,
    )
    service = _service(
        StaticGateway((quote,)),
        CountingHistoryClient(_history_bars()),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
    )
    pipelines = tuple(StaticMarketPipeline(service.eligibility, cache=cache, monotonic=lambda: 1.0) for _ in range(2))
    ready = Barrier(2)

    def initialize(pipeline):
        ready.wait(timeout=5)
        pipeline.initialize(source, NOW)
        return pipeline.observe(NOW)

    with (
        patch.object(
            static_pipeline, "NormalizeStaticIssuer", wraps=static_pipeline.NormalizeStaticIssuer
        ) as normalize,
        patch.object(
            static_pipeline, "FilterPermanentEligibility", wraps=static_pipeline.FilterPermanentEligibility
        ) as qualify,
        ThreadPoolExecutor(max_workers=2) as workers,
    ):
        first, second = tuple(workers.map(initialize, pipelines))
    assert normalize.call_count == qualify.call_count == 1
    assert first[1] == second[1] == {quote.code}
    assert first[3].records is second[3].records
    assert first[2] == second[2]
    assert all(stage.operation_durations == (("startup_reuse", None),) for stage in first[2])


def test_fixed_official_identity_survives_missing_quote_name_and_market_failure() -> None:
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
    reference = PreparedFixture(NOW, _current_universe("600001", "600002"))
    references = PublishedReferences(lambda: reference, lambda: reference.identity, lambda _: (), max_age_seconds=86400)
    history = CountingHistoryClient(_history_bars())
    service = _service(
        gateway,
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        monotonic=AdvancingClock(),
        published_references=references,
    )
    first = service.fetch_market_feature_batch(NOW)

    assert tuple(stage.stage for stage in first.static_stages) == PIPELINE_STAGES[:2]
    assert tuple((stage.input_count, stage.output_count) for stage in first.static_stages) == (
        (2, 2),
        (2, 2),
    )
    normalized = first.static_stages[0]
    assert normalized.pending_count == 0
    assert normalized.rejected_count == 0
    assert normalized.state is StageState.READY
    assert all(stage.latency_ms is None for stage in first.static_stages)
    assert all(stage.operation_durations == (("startup_reuse", None),) for stage in first.static_stages)
    assert tuple(feature.quote.code for feature in first.features) == ("600001", "600002")
    assert first.dynamic_stage.records == first.features
    assert first.dynamic_stage.snapshot.input_batch_id == first.static_stages[-1].output_batch_id
    assert first.dynamic_stage.snapshot.input_count == first.static_stages[-1].output_count
    assert first.dynamic_stage.snapshot.latency_ms > 0
    assert first.dynamic_stage.snapshot.source_health.latest_success_at == NOW
    assert history.calls == ["600001", "600002"]
    assert all(
        left.output_count == right.input_count and left.output_batch_id == right.input_batch_id
        for left, right in zip(first.static_stages, first.static_stages[1:], strict=False)
    )

    cached = service.fetch_market_feature_batch(NOW + timedelta(seconds=1))
    assert gateway.calls == 1
    assert cached.static_stages[0].operation_durations[0][0] == "startup_reuse"
    assert cached.static_stages[0].pending_count == 0
    assert cached.static_stages[0].source_health.age_seconds == 1
    assert first.static_stages[0].source_health.age_seconds == 0
    assert cached.dynamic_stage.snapshot.source_health.age_seconds == 1
    assert cached.dynamic_stage.snapshot.source_health.latest_success_at == NOW
    assert cached.dynamic_stage.snapshot.output_batch_id != first.dynamic_stage.snapshot.output_batch_id
    gateway._quotes = tuple(
        replace(quote, price=quote.price + 1, received_time=NOW + timedelta(seconds=10)) for quote in gateway._quotes
    )
    refreshed = service.fetch_market_feature_batch(NOW + timedelta(seconds=10), force=True)
    assert refreshed.static_stages[0].operation_durations[0][0] == "startup_reuse"
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
    assert retained.static_stages[0].pending_count == 0
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


def test_only_permanent_startup_facts_are_filtered_before_current_quotes() -> None:
    service = _service(
        StaticGateway((_quote(),)),
        CountingHistoryClient(_history_bars()),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
    )
    quote = _quote()
    issuers = tuple(
        StaticIssuer(code, code, quote.board, quote.exchange, quote.listing_date, index == 0, False)
        for index, code in enumerate(("600001", "600002", "600003"))
    )
    reference = StaticReferenceRead(
        StaticMarketReference(issuers, "static-company", NOW), "reference-company", False, 86400
    )
    pipeline = StaticMarketPipeline(
        service._static_pipeline.eligibility,
        cache=StaticMarketCache(),
        monotonic=lambda: 1.0,
        blacklist_codes=frozenset({"600002"}),
    )
    with pytest.raises(RuntimeError, match="static_eligibility_not_initialized"):
        pipeline.observe(NOW)
    pipeline.initialize(reference, NOW)
    batch, codes, stages, filtered = pipeline.observe(NOW)
    assert codes == {"600003"} and tuple(item.code for item in filtered.records) == ("600003",)
    assert batch.eligible_count == 1
    assert stages[1].rejected_count == 2
    assert {item.code: item.count for item in stages[1].reasons} == {
        "historical_st": 1,
        "blacklisted": 1,
    }
    assert stages[0].latency_ms is None
    cached = pipeline.observe(NOW + timedelta(seconds=2))[2][0]
    assert cached.operation_durations == (("startup_reuse", None),)
    assert cached.source_health.age_seconds == 2


def test_new_quote_st_risk_is_dynamic_and_remains_excluded_after_name_recovers(tmp_path):
    from trader.recommendation.domain.candidate.filters import HardFilter
    from trader.recommendation.infra.persistence.issuer_eligibility import SQLiteIssuerEligibilityIndex

    gateway = StaticGateway((_quote(),))
    service = _service(
        gateway,
        CountingHistoryClient(_history_bars()),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        eligibility=SQLiteIssuerEligibilityIndex(tmp_path / "eligibility.sqlite3"),
    )
    initial = service.fetch_market_feature_batch(NOW)
    at = NOW + timedelta(seconds=10)
    gateway._quotes = (replace(_quote(), name="ST公司", is_st=True, source_time=at, received_time=at),)
    blocked = service.fetch_market_feature_batch(at, force=True)
    assert blocked.static_stages[1].output_count == initial.static_stages[1].output_count == 1
    assert blocked.static_stages[1].rejected_count == 0
    assert blocked.dynamic_stage.snapshot.output_count == 1
    assert "st_or_delisting" in {
        reason.filter_code for reason in HardFilter(blocked.features[0], now=at, max_age_seconds=30).reasons
    }
    assert len(service.eligibility.facts()) == 1
    later = at + timedelta(seconds=10)
    gateway._quotes = (replace(_quote(), source_time=later, received_time=later),)
    recovered_name = service.fetch_market_feature_batch(later, force=True)
    assert "historical_st" in {
        reason.filter_code for reason in HardFilter(recovered_name.features[0], now=later, max_age_seconds=30).reasons
    }
    assert len(service.eligibility.facts()) == 1


def test_company_risk_expiry_and_coverage_are_evaluated_from_same_startup_memory():
    from trader.download.domain.company_evidence import CompanyDocument, CompanyEvidenceSnapshot, Disclosure
    from trader.recommendation.domain.candidate.filters import HardFilter

    effective = NOW
    disclosure = Disclosure("cninfo", "case", ("600001",), "收到中国证监会立案告知书", effective, "", "digest")
    snapshot = CompanyEvidenceSnapshot(
        "600001",
        NOW,
        NOW.date() - timedelta(days=365),
        NOW.date(),
        "same-publication",
        (CompanyDocument(disclosure, "本公司收到证监会立案告知书〔2026〕调查1号。", True),),
        True,
        (),
        None,
        (),
        True,
        (),
        False,
        (),
    )

    class Reader:
        visits = 0

        def publication_version(self):
            return snapshot.version

        def visit(self, codes, consume):
            self.visits += 1
            consume(snapshot)

    reader = Reader()
    service = _service(
        StaticGateway((_quote(),)),
        CountingHistoryClient(_history_bars()),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        company_evidence_reader=reader,
    )
    before = service.fetch_market_feature_batch(NOW)
    active = service.fetch_market_feature_batch(effective + timedelta(seconds=10))
    expired_at = effective + timedelta(days=365, microseconds=1)
    expired = service.fetch_market_feature_batch(expired_at)
    assert before.features[0].values["official_investigation_history"] == 1
    assert active.features[0].values["official_investigation_history"] == 1
    assert expired.features[0].values["official_investigation_history"] == 0
    assert "research_data_degraded" in expired.features[0].quote.execution_restrictions
    assert "official_investigation_history" in {
        reason.filter_code for reason in HardFilter(active.features[0], now=effective, max_age_seconds=30).reasons
    }
    assert before.static_stages[1].output_count == active.static_stages[1].output_count == 1
    assert before.static_stages[1].output_batch_id == expired.static_stages[1].output_batch_id
    assert reader.visits == 1


def test_future_permanent_fact_takes_effect_dynamically_without_rebuilding_static_snapshot(tmp_path):
    from trader.recommendation.domain.candidate.filters import HardFilter
    from trader.recommendation.domain.market.eligibility import ManualBlacklistFact
    from trader.recommendation.infra.persistence.issuer_eligibility import SQLiteIssuerEligibilityIndex

    eligibility = SQLiteIssuerEligibilityIndex(tmp_path / "eligibility.sqlite3")
    effective = NOW + timedelta(seconds=10)
    eligibility.record((ManualBlacklistFact("600001", effective_at=effective, config_hash="future"),))
    service = _service(
        StaticGateway((_quote(),)),
        CountingHistoryClient(_history_bars()),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        eligibility=eligibility,
    )
    before = service.fetch_market_feature_batch(NOW)
    after = service.fetch_market_feature_batch(effective)
    assert not any(value.startswith("issuer_risk:") for value in before.features[0].quote.execution_restrictions)
    assert "manual_permanent_blacklist" in {
        reason.filter_code for reason in HardFilter(after.features[0], now=effective, max_age_seconds=30).reasons
    }
    assert before.static_stages[1].output_batch_id == after.static_stages[1].output_batch_id
    assert after.static_stages[1].rejected_count == 0
