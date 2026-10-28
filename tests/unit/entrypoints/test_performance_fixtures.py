"""Profile-complete synthetic workload checks without fitting or external I/O."""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from trader.infra.serialization.canonical import ContentHash
from trader.infra.settings.models import (
    PerformanceBudgetSettings,
    DatabasePerformanceSettings,
    PerformanceMemorySettings,
    PerformanceRoundSettings,
    PerformanceWorkloadSettings,
)
from trader.recommendation.application.pipeline.local_score.model_scoring import ProductionModelScoringService
from trader.recommendation.application.pipeline.local_score.v1.scoring import LearnedRankScoringService
from trader.recommendation.application.ports.loaded_profile import (
    HeadRuntime,
    LoadedScoringProfile,
    ModelInput,
    ModelPrediction,
    ProfileEvidence,
)
from trader.recommendation.domain.market.v1.features import LEARNED_RANK_FEATURE_IDS
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.domain.scoring.residualization import TRAINED_HEAD_EXPOSURE_CONTRACT
from trader.recommendation.domain.selection.execution_cost import ExecutionCostPolicy
from trader.recommendation.infra.performance.fixtures import (
    BuildPerformanceFixtures,
    BuildWorstCaseCandidatePlans,
    CompleteRealtimeObservations,
    ProjectCandidateUnionQuotes,
)
from trader.training.infra.profile.composition import SingleHeadCombiner
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE


@dataclass(frozen=True)
class _Predictor:
    profile_id: ScoringProfileId
    feature_ids: tuple[str, ...]
    industry_ids: tuple[str, ...] = ("fixture-bank", "fixture-energy")
    model_id: str = "performance-model"
    model_hash: str = "a" * 64
    exposure_contract = TRAINED_HEAD_EXPOSURE_CONTRACT

    def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
        raise AssertionError("fixture qualification must not call prediction")


def _profile(profile_id: ScoringProfileId) -> LoadedScoringProfile:
    contracts = {"v2": V2_TRAINING_PROFILE, "v3": V3_TRAINING_PROFILE, "v5": V5_TRAINING_PROFILE}
    heads: dict[Strategy, HeadRuntime] = {}
    for strategy in (Strategy.TOMORROW, Strategy.D25):
        names = (
            LEARNED_RANK_FEATURE_IDS
            if profile_id == "v1"
            else tuple(
                name
                for name in contracts[profile_id].head_for_strategy(strategy).feature_manifest.names
                if not name.startswith("sequence_")
            )
        )
        heads[strategy] = HeadRuntime(
            strategy,
            _Predictor(profile_id, names),
            SingleHeadCombiner(),
            ProfileEvidence("historical_unavailable", (), "trained_artifact", production_authority=profile_id != "v1"),
        )
    return LoadedScoringProfile(profile_id, heads)


def _budgets() -> PerformanceBudgetSettings:
    return PerformanceBudgetSettings(
        PerformanceWorkloadSettings(12, 12),
        PerformanceRoundSettings(1, 5),
        {},
        {},
        PerformanceMemorySettings(536870912, 2147483648, 5.0),
        10.0,
        DatabasePerformanceSettings(10.0, 15.0, 4),
    )


@pytest.mark.parametrize("profile_id,sessions", (("v1", 251), ("v2", 251), ("v3", 61), ("v5", 62)))
def test_profile_fixtures_qualify_for_both_actual_scoring_services(profile_id, sessions):
    profile = _profile(profile_id)
    inputs, quotes, features = BuildPerformanceFixtures(_budgets(), profile)
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
    assert BuildPerformanceFixtures(_budgets(), profile) == (inputs, quotes, features)


def test_industry_fixture_rejects_disjoint_head_coverage():
    profile = _profile("v3")
    head = profile.heads[Strategy.D25]
    different = replace(head, predictor=replace(head.predictor, industry_ids=("other",)))
    profile = replace(profile, heads={**profile.heads, Strategy.D25: different})
    with pytest.raises(ValueError, match="common_industry_coverage_missing"):
        BuildPerformanceFixtures(_budgets(), profile)


def test_candidate_union_and_realtime_observations_keep_fixture_codes():
    _, quotes, _ = BuildPerformanceFixtures(_budgets(), _profile("v3"))
    plans = BuildWorstCaseCandidatePlans(len(quotes), quotes[0].received_time)
    assert ProjectCandidateUnionQuotes(quotes, plans) == quotes
    observations = CompleteRealtimeObservations(quotes, quotes[0].received_time)
    assert len(observations) == 2 * len(quotes)
    assert {item.source for item in observations} == {"tencent_market", "sina"}


def test_full_market_fixture_quote_structure_is_valid_on_all_boards():
    budgets = replace(_budgets(), workload=PerformanceWorkloadSettings(5500, 5500))
    _, quotes, _ = BuildPerformanceFixtures(budgets, _profile("v3"))
    assert {quote.board.value for quote in quotes} == {"main", "chinext", "star"}
    assert all(
        quote.low <= quote.open_price <= quote.high and quote.low <= quote.price <= quote.high for quote in quotes
    )
    assert all(quote.pct_change == pytest.approx((quote.price / quote.previous_close - 1) * 100) for quote in quotes)
