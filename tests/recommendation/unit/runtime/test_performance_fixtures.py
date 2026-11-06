from __future__ import annotations

from dataclasses import replace


import pytest

from trader.infra.serialization.canonical import ContentHash

from trader.infra.settings.models import PerformanceWorkloadSettings

from trader.recommendation.application.pipeline.local_score.model_scoring import ProductionModelScoringService

from trader.recommendation.application.pipeline.local_score.v1.scoring import LearnedRankScoringService



from trader.recommendation.domain.publication.models import Strategy



from trader.recommendation.domain.selection.execution_cost import ExecutionCostPolicy

from trader.recommendation.infra.performance.fixtures import (
    BuildPerformanceFixtures,
    BuildWorstCaseCandidatePlans,
    CompleteRealtimeObservations,
    ProjectCandidateUnionQuotes,
)






from tests.helpers.recommendation.runtime.performance_fixtures import (
    _budgets,
    _history_sessions,
    _profile,
)


@pytest.mark.parametrize("profile_id", ("v1", "v2", "v3", "v5"))
def test_profile_fixtures_qualify_for_both_actual_scoring_services(profile_id):
    profile = _profile(profile_id)
    sessions = _history_sessions(profile_id)
    inputs, quotes, features = BuildPerformanceFixtures(
        _budgets(), profile, history_sessions=_history_sessions(profile_id)
    )
    assert len(inputs) == len(quotes) == len(features) == 12
    assert len({feature.quote.code for feature in features}) == 12
    assert all(feature.history_days == sessions for feature in features)
    assert tuple(item.industry for item in inputs) == tuple(item.industry for item in quotes)
    assert all(feature.model_history_quality.failure is None for feature in features)
    for strategy in (Strategy.TOMORROW, Strategy.D25):
        service = (
            LearnedRankScoringService(
                profile, strategy, cost_policy=ExecutionCostPolicy(0.002, 1.0), content_hash=ContentHash
            )
            if profile_id == "v1"
            else ProductionModelScoringService(profile, strategy, cost_policy=ExecutionCostPolicy(0.002, 1.0))
        )
        assert all(service.is_input_eligible(feature) for feature in features)
    if profile_id == "v5":
        for feature in features:
            sequence = feature.model_sequence
            assert sequence.code == feature.quote.code
            assert sequence.dates[-1] < feature.observed_at.date()
            assert len(sequence.dates) == 61 and len(sequence.values) == 360
            assert len(set(sequence.values)) > 6
    else:
        assert all(feature.model_sequence is None for feature in features)
    assert BuildPerformanceFixtures(
        _budgets(), profile, history_sessions=_history_sessions(profile_id)
    ) == (inputs, quotes, features)


def test_industry_fixture_rejects_disjoint_head_coverage():
    profile = _profile("v3")
    head = profile.heads[Strategy.D25]
    different = replace(head, predictor=replace(head.predictor, industry_ids=("other",)))
    profile = replace(profile, heads={**profile.heads, Strategy.D25: different})
    with pytest.raises(ValueError, match="common_industry_coverage_missing"):
        BuildPerformanceFixtures(_budgets(), profile, history_sessions=_history_sessions("v2"))


def test_candidate_union_and_realtime_observations_keep_fixture_codes():
    _, quotes, _ = BuildPerformanceFixtures(
        _budgets(), _profile("v3"), history_sessions=_history_sessions("v3")
    )
    plans = BuildWorstCaseCandidatePlans(len(quotes), quotes[0].received_time)
    assert ProjectCandidateUnionQuotes(quotes, plans) == quotes
    observations = CompleteRealtimeObservations(quotes, quotes[0].received_time)
    assert len(observations) == 2 * len(quotes)
    assert {item.source for item in observations} == {"tencent_market", "sina"}


def test_full_market_fixture_quote_structure_is_valid_on_all_boards():
    budgets = replace(_budgets(), workload=PerformanceWorkloadSettings(5500, 5500))
    _, quotes, _ = BuildPerformanceFixtures(
        budgets, _profile("v3"), history_sessions=_history_sessions("v3")
    )
    assert {quote.board.value for quote in quotes} == {"main", "chinext", "star"}
    assert all(
        quote.low <= quote.open_price <= quote.high and quote.low <= quote.price <= quote.high for quote in quotes
    )
    assert all(quote.pct_change == pytest.approx((quote.price / quote.previous_close - 1) * 100) for quote in quotes)


def test_performance_fixture_uses_configured_market_and_candidate_populations():
    budgets = replace(_budgets(), workload=PerformanceWorkloadSettings(12, 5))

    inputs, quotes, features = BuildPerformanceFixtures(
        budgets, _profile("v3"), history_sessions=_history_sessions("v3")
    )

    assert len(inputs) == len(quotes) == 12
    assert len(features) == 5
    assert tuple(feature.quote.code for feature in features) == tuple(quote.code for quote in quotes[:5])
