from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta

from tests.helpers.recommendation.publication.tomorrow_projection import (
    EVALUATED_AT,
    _ProductionPredictor,
    _verified_feature,
    _with_model_features,
)
from tests.helpers.recommendation.runtime.input_runtime import _policy
from tests.helpers.recommendation.scoring_helpers import BuildUtilityScorer, profile_for
from trader.recommendation.application.pipeline.dynamic_filter.filter_executor import (
    FilterFeatureCandidates,
    ScoredSelectionIdentity,
    ScoredSelectionOptions,
)
from trader.recommendation.application.pipeline.final_selection.company_replay import (
    CompanyReplayInputs,
    CompanyReplayPublication,
    ProjectCompanyReplayDecision,
)
from trader.recommendation.application.pipeline.final_selection.freeze_projection import (
    BuildScoredLocal,
    ScoredLocalProjection,
    ScoredProjectionInputs,
)
from trader.recommendation.application.pipeline.local_score.model_router import ModelScoringRouter
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.application.ports.loaded_profile import ModelInput, ModelPrediction
from trader.recommendation.application.ports.scoring import D25NativeInput
from trader.recommendation.domain.candidate.filters import QuoteFreshnessBasis
from trader.recommendation.domain.market.models import Board, FeatureSnapshot
from trader.recommendation.domain.market.research import ResearchComponentCoverage
from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.domain.selection.execution_cost import ExecutionCostPolicy


class _D25Predictor(_ProductionPredictor):
    def __init__(self, profile: ScoringProfileId) -> None:
        self.profile_id = profile

    def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
        return tuple(replace(prediction, target_probability=0.60) for prediction in super().predict(inputs))


def _with_financial(feature: FeatureSnapshot, score: float) -> FeatureSnapshot:
    return replace(feature, values={
        **feature.values,
        "quality_score": score,
        "value_score": score,
        "growth_score": score,
    })


def BuildCompanyReplayCase(
    feature_factory: Callable[[str, datetime], FeatureSnapshot],
    *,
    profile: ScoringProfileId = "v3",
    evaluated_at: datetime = EVALUATED_AT,
) -> tuple[CompanyReplayInputs, RecommendationPolicy, ScoredProjectionInputs, ScoredLocalProjection]:
    strategy = Strategy.D25
    policy = _policy()
    weights = {
        board: {name: float(name == "quality_value") for name in policy.board_local_strategy_weights[strategy][board]}
        for board in (Board.MAIN, Board.CHINEXT, Board.STAR)
    }
    policy = replace(policy, board_local_strategy_weights={**policy.board_local_strategy_weights, strategy: weights})
    covered = ResearchComponentCoverage("known_clear", "known_clear", "known_clear", "known_clear")
    uncertain = ResearchComponentCoverage("known_clear", "unknown", "known_clear", "unknown")
    points = (94.0, 83.0, 78.0, 72.0, 67.0, 60.0, 58.0, 54.0, 52.0)
    covered_at = evaluated_at - timedelta(seconds=10)
    codes = ("600000", "600001", "600002", "600003", "300000", "300001", "688000", "688001", "688002")
    rows = tuple(
        _with_financial(replace(
            _verified_feature(_with_model_features(feature_factory(code, covered_at), index)),
            research_components=covered,
        ), points[index])
        for index, code in enumerate(codes)
    )
    first, second, *remaining, last = rows
    baseline_last = replace(last, values={**last.values, "latest_disclosed_report_loss": 1.0})
    baseline_rows = (*rows[:-1], baseline_last)
    changed_first = replace(first, values={**first.values, "latest_disclosed_report_loss": 1.0})
    improved_second = _with_financial(second, 100.0)
    changed_second = replace(
        improved_second,
        research_components=uncertain,
        values={
            **improved_second.values,
            "corporate_risk_window_unavailable": 1.0,
            "entry_quality": 0.0,
        },
    )
    changed_last = replace(last, values={**last.values, "latest_disclosed_report_loss": 0.0})
    changed_rows = (changed_first, changed_second, *remaining, changed_last)
    baseline = D25NativeInput(
        trade_date=evaluated_at.date(),
        phase="final_review",
        data_version="candidate-data:fixture",
        config_version="runtime-current+strategy-fixture",
        evaluated_at=evaluated_at,
        market_features=baseline_rows,
        requested_codes=codes[:-1],
        candidate_features=baseline_rows[:-1],
        preselect_max_age_seconds=30.0,
        score_max_age_seconds=30.0,
    )
    candidate = replace(baseline, market_features=changed_rows, candidate_features=changed_rows[:-1])
    discovery = FilterFeatureCandidates(
        baseline_rows,
        policy,
        ScoredSelectionOptions(
            evaluated_at=covered_at,
            max_age_seconds=30.0,
            phase="candidate_discovery",
            strategy=strategy,
            population_quote_freshness_basis=QuoteFreshnessBasis.RECEIPT,
        ),
        ScoredSelectionIdentity(covered_at.date(), baseline.data_version, baseline.data_version),
    )

    def runtime() -> ScoredProjectionInputs:
        scorer = BuildUtilityScorer(
            profile_for(_D25Predictor(profile), strategy),
            strategy,
            cost_policy=ExecutionCostPolicy(0.002, 1.0),
            prediction_target=PredictionTarget(strategy, 100),
        )
        return ScoredProjectionInputs(
            model_scoring=ModelScoringRouter(profile, {strategy: scorer}),
            population_qualifications=discovery,
        )

    observed = BuildScoredLocal(baseline, policy, sequence=4, runtime=runtime())
    inputs = CompanyReplayInputs(
        baseline,
        candidate,
        CompanyReplayPublication("company-current", covered_at - timedelta(days=2)),
        CompanyReplayPublication("company-candidate", covered_at - timedelta(days=1)),
        ProjectCompanyReplayDecision(observed),
    )
    return inputs, policy, runtime(), observed
