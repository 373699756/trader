"""Offline candidates retain business results and explicit resource boundaries."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
import threading

import pytest

from tests.helpers.recommendation.market_data import _history_bars
from tests.helpers.recommendation.runtime.recommendation_boundaries import _features, _runtime_policy
from tests.helpers.recommendation.publication.tomorrow_projection import EVALUATED_AT, _verified_feature
from trader.infra.cache import BoundedLruCache
from trader.recommendation.domain.market.history_metrics import BuildHistoryContext
from trader.infra.settings import LoadRuntimeSettings
from trader.recommendation.application.pipeline.dynamic_filter.filter_executor import ScoredSelectionIdentity, ScoredSelectionOptions
from trader.recommendation.application.pipeline.final_selection.grouped_ranking import RankingSelectionService
from trader.recommendation.application.ports.loaded_profile import ModelInput, ModelPrediction
from trader.recommendation.infra.performance.history_context_reuse import ReusableHistoryContexts
from trader.recommendation.infra.performance.industry_prediction import ParallelIndustryPrediction
from trader.recommendation.infra.performance.scoring_inputs_reuse import ScoringInputsReuse

from tests.helpers.recommendation.publication.tomorrow_projection import _native_input
from trader.recommendation.application.pipeline.dynamic_filter.filter_executor import FilterFeatureCandidates
from trader.recommendation.application.pipeline.final_selection.freeze_projection import BuildScoredLocal, ScoredProjectionInputs
from trader.recommendation.domain.candidate.filters import QuoteFreshnessBasis
from trader.recommendation.domain.evidence.review import RiskFact
from trader.recommendation.domain.market.models import Evidence
from trader.recommendation.domain.publication.models import ScoredDisposition
from trader.recommendation.domain.selection.scored_selection import BuildScoredCandidateReferences, PreselectionReplayFeature


@pytest.fixture
def scoring_cache():
    policy = LoadRuntimeSettings(Path("config/runtime.json")).market_data.cache_policy
    cache = BoundedLruCache(policy, wall_clock=lambda: EVALUATED_AT)
    yield cache
    cache.stop()


@pytest.mark.parametrize("change", ("none", "risk", "quote", "time", "policy", "model_gate"))
def test_scoring_reuse_tracks_complete_inputs_and_matches_original(scoring_cache, application_feature_factory, change):
    features = _features(application_feature_factory)
    policy = _runtime_policy()
    options = ScoredSelectionOptions(evaluated_at=EVALUATED_AT, max_age_seconds=30.0)
    identity = ScoredSelectionIdentity(EVALUATED_AT.date(), "batch", "epoch")
    candidate = ScoringInputsReuse(scoring_cache)
    first = candidate.select(features, policy, options, identity)
    gates = None
    if change == "risk":
        features = (replace(features[0], values={**features[0].values, "pledge_risk": 100.0}), *features[1:])
    elif change == "quote":
        features = (replace(features[0], quote=replace(features[0].quote, price=13.0)), *features[1:])
    elif change == "time":
        options = replace(options, evaluated_at=EVALUATED_AT + timedelta(seconds=31))
    elif change == "policy":
        policy = replace(policy, strategy_version="changed-policy")
    elif change == "model_gate":
        gates = {features[0].quote.code: "model_net_utility_nonpositive"}
    second = candidate.select(features, policy, options, identity, execution_gate_reasons=gates)
    expected = RankingSelectionService().select(features, policy, options, identity, execution_gate_reasons=gates)
    assert second.selection == expected.selection
    assert candidate.counts.calculations == (1 if change == "none" else 2)
    assert candidate.counts.hits == int(change == "none")
    if change == "none":
        assert first.selection == second.selection
    assert second.operation_durations[0][0] == "preparation_identity_and_cache"


def test_scoring_reuse_coalesces_and_budget_rejection_keeps_full_result(scoring_cache, application_feature_factory, monkeypatch):
    features, policy = _features(application_feature_factory), _runtime_policy()
    options = ScoredSelectionOptions(evaluated_at=EVALUATED_AT, max_age_seconds=30.0)
    identity = ScoredSelectionIdentity(EVALUATED_AT.date(), "batch", "epoch")
    candidate = ScoringInputsReuse(scoring_cache)
    with ThreadPoolExecutor(max_workers=4) as executor:
        outputs = tuple(executor.map(lambda _: candidate.select(features, policy, options, identity), range(4)))
    assert all(item.selection == outputs[0].selection for item in outputs)
    assert candidate.counts.calculations == 1 and candidate.counts.hits == 3
    identity = replace(identity, merge_epoch="not-cached")
    monkeypatch.setattr(scoring_cache, "put", lambda *args, **kwargs: False)
    first = candidate.select(features, policy, options, identity)
    second = candidate.select(features, policy, options, identity)
    assert first.selection == second.selection
    assert candidate.counts.calculations == 3


def test_failed_scoring_preparation_is_never_cached(scoring_cache, application_feature_factory, monkeypatch):
    import trader.recommendation.infra.performance.scoring_inputs_reuse as module

    features, policy = _features(application_feature_factory), _runtime_policy()
    options = ScoredSelectionOptions(evaluated_at=EVALUATED_AT, max_age_seconds=30.0)
    identity = ScoredSelectionIdentity(EVALUATED_AT.date(), "batch", "epoch")
    original = module.BuildScoredCandidateReferences
    candidate = ScoringInputsReuse(scoring_cache)

    def fail(_qualified):
        raise RuntimeError("cancelled")

    monkeypatch.setattr(module, "BuildScoredCandidateReferences", fail)
    with pytest.raises(RuntimeError, match="cancelled"):
        candidate.select(features, policy, options, identity)
    monkeypatch.setattr(module, "BuildScoredCandidateReferences", original)
    assert candidate.select(features, policy, options, identity).selection.evaluations
    assert candidate.counts.calculations == 1


@pytest.mark.parametrize("sessions", (0, 1, 20, 60, 61, 120, 251))
def test_preload_metrics_reuse_preserves_every_context_field_and_window_identity(sessions):
    seed = _history_bars()
    bars = tuple(replace(seed[index % len(seed)], trade_date=(date(2025, 1, 1) + timedelta(days=index)).isoformat()) for index in range(sessions))
    candidate = ReusableHistoryContexts()
    for lookback in (61, 251, 20, 1, 251):
        assert candidate.build(tuple(reversed(bars)), lookback) == BuildHistoryContext(bars, lookback_sessions=lookback)
        assert candidate.counts.entries <= 2
    candidate.clear()
    assert candidate.counts.entries == 0


def test_preload_long_and_short_windows_reuse_metrics_without_reusing_anchors():
    seed = _history_bars()
    bars = tuple(replace(seed[index % len(seed)], trade_date=(date(2025, 1, 1) + timedelta(days=index)).isoformat()) for index in range(251))
    candidate = ReusableHistoryContexts()
    short, long = candidate.build(bars, 61), candidate.build(bars, 251)
    assert short == BuildHistoryContext(bars, lookback_sessions=61)
    assert long == BuildHistoryContext(bars, lookback_sessions=251)
    assert short.sample_count == 61 and long.sample_count == 251
    assert short.return_anchors != long.return_anchors
    assert candidate.counts.requests == 4 and candidate.counts.calculations == candidate.counts.hits == 2


@pytest.mark.parametrize("failure", (None, RuntimeError, KeyboardInterrupt))
def test_industry_parallelism_is_bounded_ordered_and_drained_on_failure(failure):
    barrier = threading.Barrier(2)
    groups = tuple((ModelInput(f"{index:06d}", (1.0,), industry),) for index, industry in enumerate(("bank", "software")))

    def calculate(group):
        barrier.wait(2.0)
        if group[0].industry == "bank" and failure is not None:
            raise failure("injected")
        return tuple(ModelPrediction(item.code, 0.01) for item in group)

    with ThreadPoolExecutor(max_workers=2) as executor:
        candidate = ParallelIndustryPrediction(executor)
        if failure is not None:
            with pytest.raises(failure, match="injected"):
                candidate.predict(calculate, groups)
        else:
            results = candidate.predict(calculate, groups)
            assert tuple(group[0].code for group in results) == ("000000", "000001")
        assert candidate.counts.in_flight == 0
        assert candidate.counts.peak_in_flight == 2
        assert candidate.counts.failed == int(failure is not None)


def test_parallel_candidate_rejects_same_industry_groups_before_submitting():
    groups = ((ModelInput("000001", (1.0,), "same"),), (ModelInput("000002", (1.0,), "same"),))
    with ThreadPoolExecutor(max_workers=2) as executor:
        candidate = ParallelIndustryPrediction(executor)
        with pytest.raises(ValueError, match="groups_invalid"):
            candidate.predict(lambda _: (), groups)
        assert candidate.counts.completed == candidate.counts.peak_in_flight == 0


@pytest.mark.parametrize("change", ("none_candidates", "empty_candidates", "quote", "value", "filter_value", "history"))
def test_frozen_population_requires_exact_projection_and_independent_candidates(application_feature_factory, change):
    original = _features(application_feature_factory)
    policy = _runtime_policy()
    features = tuple(PreselectionReplayFeature(item, hard_filter=policy.hard_filter) for item in original)
    identity = ScoredSelectionIdentity(EVALUATED_AT.date(), "batch", "epoch")
    qualifications = FilterFeatureCandidates(
        original,
        policy,
        ScoredSelectionOptions(
            evaluated_at=EVALUATED_AT,
            max_age_seconds=30.0,
            phase="candidate_discovery",
            population_quote_freshness_basis=QuoteFreshnessBasis.RECEIPT,
        ),
        identity,
    )
    candidates = None if change == "none_candidates" else () if change == "empty_candidates" else original[:2]
    if change == "quote":
        features = (replace(features[0], quote=replace(features[0].quote, price=13.0)), *features[1:])
    elif change == "value":
        features = (replace(features[0], values={**features[0].values, "trend_score": 123.0}), *features[1:])
    elif change == "filter_value":
        features = (replace(features[0], values={**features[0].values, "latest_disclosed_report_loss": 1.0}), *features[1:])
    elif change == "history":
        features = (replace(features[0], history_days=features[0].history_days + 1), *features[1:])
    options = ScoredSelectionOptions(
        evaluated_at=EVALUATED_AT, max_age_seconds=30.0, candidate_features=candidates,
    )
    if change == "empty_candidates":
        result = RankingSelectionService().select(
            features, policy, options, identity, population_qualifications=qualifications,
        )
        assert not result.selection.scored_candidates
        return
    with pytest.raises(ValueError, match="independently evaluated candidates|preselection projection"):
        RankingSelectionService().select(
            features, policy, options, identity, population_qualifications=qualifications,
        )


@pytest.mark.parametrize("risk_field", ("latest_disclosed_report_loss", "pledge_risk", "financial_fraud_history"))
@pytest.mark.parametrize("risk_state", ("positive", "zero", "unknown"))
def test_frozen_population_replays_risk_inputs_and_preserves_qualified_denominator(
    application_feature_factory, risk_field, risk_state,
):
    policy = _runtime_policy()
    original = tuple(
        _verified_feature(application_feature_factory(f"600{index:03d}", EVALUATED_AT - timedelta(seconds=10)))
        for index in range(40)
    )
    value = policy.hard_filter.structured_risk_thresholds[risk_field] + 1.0 if risk_state == "positive" else (
        None if risk_state == "unknown" else 0.0
    )
    original = (*original[:-1], replace(original[-1], values={**original[-1].values, risk_field: value}))
    identity = ScoredSelectionIdentity(EVALUATED_AT.date(), "batch", "epoch")
    qualifications = FilterFeatureCandidates(
        original,
        policy,
        ScoredSelectionOptions(
            evaluated_at=EVALUATED_AT,
            max_age_seconds=30.0,
            phase="candidate_discovery",
            population_quote_freshness_basis=QuoteFreshnessBasis.RECEIPT,
        ),
        identity,
    )
    projected = tuple(PreselectionReplayFeature(item, hard_filter=policy.hard_filter) for item in original)
    replayed = FilterFeatureCandidates(
        projected,
        policy,
        ScoredSelectionOptions(
            evaluated_at=EVALUATED_AT, max_age_seconds=30.0, candidate_features=original[:-1],
        ),
        identity,
        population_qualifications=qualifications,
    )

    for code, before in qualifications.population.evaluations.items():
        after = replayed.population.evaluations[code]
        assert (after.disposition, after.filter_reasons, after.optional_flags) == (
            before.disposition, before.filter_reasons, before.optional_flags,
        )
    expected_size = 39 if risk_state == "positive" else 40
    for filtered in (qualifications, replayed):
        references = BuildScoredCandidateReferences(filtered)
        assert tuple(item.population.sample_size for item in references.cross_sections) == (expected_size,)
    last = replayed.population.evaluations[original[-1].quote.code]
    assert last.features.values[risk_field] == value
    if risk_state == "positive":
        assert last.disposition is ScoredDisposition.REJECT
        assert risk_field in {item.code for item in last.filter_reasons}
    elif risk_state == "unknown":
        assert last.disposition is ScoredDisposition.OBSERVE_ONLY
        assert "structured_risk_unavailable" in {item.code for item in last.optional_flags}


def test_frozen_population_keeps_candidate_supplement_and_review_risk_separate(application_feature_factory):
    policy = _runtime_policy()
    original = tuple(
        _verified_feature(application_feature_factory(f"600{index:03d}", EVALUATED_AT - timedelta(seconds=10)))
        for index in range(40)
    )
    identity = ScoredSelectionIdentity(EVALUATED_AT.date(), "batch", "epoch")
    qualifications = FilterFeatureCandidates(
        original,
        policy,
        ScoredSelectionOptions(
            evaluated_at=EVALUATED_AT,
            max_age_seconds=30.0,
            phase="candidate_discovery",
            population_quote_freshness_basis=QuoteFreshnessBasis.RECEIPT,
        ),
        identity,
    )
    supplemented = replace(
        original[-1],
        values={**original[-1].values, "latest_disclosed_report_loss": 1.0},
        evidence=(Evidence("announcement", "announcement", "候选补查公告", "fixture", EVALUATED_AT),),
        external_risk_facts=(RiskFact("review", "financial_deterioration", "high", 0.0, "deepseek", EVALUATED_AT, veto=True),),
    )
    projection = PreselectionReplayFeature(supplemented, hard_filter=policy.hard_filter)
    assert not projection.evidence and not projection.external_risk_facts
    assert "news_sentiment" not in projection.values
    assert projection.values["latest_disclosed_report_loss"] == 1.0
    result = RankingSelectionService().select(
        tuple(PreselectionReplayFeature(item, hard_filter=policy.hard_filter) for item in original),
        policy,
        ScoredSelectionOptions(
            evaluated_at=EVALUATED_AT, max_age_seconds=30.0, candidate_features=(*original[:-1], supplemented),
        ),
        identity,
        population_qualifications=qualifications,
    )

    assert result.selection.population_rejected_count == 0
    assert not result.selection.population_filter_reason_counts
    rejected = next(item for item in result.selection.evaluations if item.code == supplemented.quote.code)
    assert rejected.disposition is ScoredDisposition.REJECT
    assert "latest_disclosed_report_loss" in {item.code for item in rejected.filter_reasons}
    assert supplemented.quote.code not in {item.code for item in result.selection.scored_candidates}
    assert original[-1].values["latest_disclosed_report_loss"] == 0.0

    native = replace(_native_input(original), candidate_features=original[:-1])
    local = BuildScoredLocal(
        native, policy, sequence=1, runtime=ScoredProjectionInputs(population_qualifications=qualifications),
    )
    assert not local.selection.population_rejected_count
    assert not local.selection.population_filter_reason_counts


def test_scoring_reuse_supports_local_projection_consumer(scoring_cache, application_feature_factory):
    native = _native_input(_features(application_feature_factory))
    policy = _runtime_policy()
    expected = BuildScoredLocal(native, policy, sequence=1)
    actual = BuildScoredLocal(
        native, policy, sequence=1, runtime=ScoredProjectionInputs(ranking_selection=ScoringInputsReuse(scoring_cache)),
    )
    assert actual.selection == expected.selection
    assert actual.local == expected.local


def test_frozen_population_cannot_revive_an_old_day_through_its_source_watermark(application_feature_factory):
    current = _features(application_feature_factory)
    previous_at = EVALUATED_AT - timedelta(days=1)
    previous = tuple(
        replace(item, observed_at=previous_at, quote=replace(
            item.quote, source_time=previous_at, received_time=previous_at,
        ))
        for item in current
    )
    policy = _runtime_policy()
    identity = ScoredSelectionIdentity(EVALUATED_AT.date(), "old-market", "epoch")
    qualifications = FilterFeatureCandidates(
        previous,
        policy,
        ScoredSelectionOptions(
            evaluated_at=EVALUATED_AT,
            max_age_seconds=30.0,
            phase="candidate_discovery",
            population_quote_freshness_basis=QuoteFreshnessBasis.RECEIPT,
        ),
        identity,
    )
    assert all(
        any(reason.code == "stale_quote" for reason in item.filter_reasons)
        for item in qualifications.population.evaluations.values()
    )

    result = RankingSelectionService().select(
        tuple(PreselectionReplayFeature(item, hard_filter=policy.hard_filter) for item in previous),
        policy,
        ScoredSelectionOptions(
            evaluated_at=EVALUATED_AT,
            max_age_seconds=30.0,
            population_evaluated_at=previous_at,
            candidate_features=current[:2],
        ),
        identity,
        population_qualifications=qualifications,
    )

    assert result.selection.population_filter_reason_counts["stale_quote"] == len(previous)
    assert not result.selection.scored_candidates


@pytest.mark.parametrize("identity_failure", (False, True))
def test_scoring_reuse_binds_frozen_population_in_cache_and_fallback(
    scoring_cache, application_feature_factory, monkeypatch, identity_failure,
):
    import trader.recommendation.infra.performance.scoring_inputs_reuse as module

    original = _features(application_feature_factory)
    population = tuple(
        replace(item, quote=replace(
            item.quote,
            source_time=EVALUATED_AT - timedelta(seconds=45),
            received_time=EVALUATED_AT - timedelta(seconds=45),
        ))
        for item in original
    )
    policy = _runtime_policy()
    features = tuple(PreselectionReplayFeature(item, hard_filter=policy.hard_filter) for item in population)
    identity = ScoredSelectionIdentity(EVALUATED_AT.date(), "batch", "epoch")
    qualifications = FilterFeatureCandidates(
        population,
        policy,
        ScoredSelectionOptions(
            evaluated_at=EVALUATED_AT,
            max_age_seconds=30.0,
            phase="candidate_discovery",
            population_quote_freshness_basis=QuoteFreshnessBasis.RECEIPT,
        ),
        identity,
    )
    options = ScoredSelectionOptions(
        evaluated_at=EVALUATED_AT, max_age_seconds=30.0, candidate_features=original[:2],
    )
    candidate = ScoringInputsReuse(scoring_cache)
    unqualified = candidate.select(features, policy, options, identity)
    if identity_failure:
        def reject_identity(_value):
            raise ValueError("unsupported_identity")

        monkeypatch.setattr(module, "CanonicalDigest", reject_identity)
    actual = candidate.select(
        features, policy, options, identity, population_qualifications=qualifications,
    )
    expected = RankingSelectionService().select(
        features, policy, options, identity, population_qualifications=qualifications,
    )
    assert actual.selection == expected.selection
    assert actual.selection != unqualified.selection
    assert actual.selection.population_filter_reason_counts == {}
    if identity_failure:
        assert candidate.counts.bypasses == 1
    else:
        assert candidate.counts.calculations == 2 and candidate.counts.hits == 0
        assert candidate.select(
            features, policy, options, identity, population_qualifications=qualifications,
        ).selection == expected.selection
        assert candidate.counts.calculations == 2 and candidate.counts.hits == 1
