"""Offline candidates retain business results and explicit resource boundaries."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
import threading

import pytest

from tests.component.market_data_test_support import _history_bars
from tests.unit.application.test_recommendation_boundaries import _features, _runtime_policy
from tests.unit.application.test_tomorrow_projection import EVALUATED_AT
from trader.infra.cache import BoundedLruCache
from trader.infra.market_data.history.history import BuildHistoryContext
from trader.infra.settings import LoadRuntimeSettings
from trader.recommendation.application.pipeline.dynamic_filter.filter_executor import ScoredSelectionIdentity, ScoredSelectionOptions
from trader.recommendation.application.pipeline.final_selection.grouped_ranking import RankingSelectionService
from trader.recommendation.application.ports.loaded_profile import ModelInput, ModelPrediction
from trader.recommendation.infra.performance.history_context_reuse import ReusableHistoryContexts
from trader.recommendation.infra.performance.industry_prediction import ParallelIndustryPrediction
from trader.recommendation.infra.performance.scoring_inputs_reuse import ScoringInputsReuse


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
