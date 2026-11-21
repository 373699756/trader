from __future__ import annotations

import time
from collections.abc import Callable

from trader.recommendation.application.d25.v2.plan import V2_D25_PLAN
from trader.recommendation.application.d25.v3.plan import V3_D25_PLAN
from trader.recommendation.application.d25.v5.plan import V5_D25_PLAN
from trader.recommendation.application.pipeline.local_score.model_scoring import ProductionModelScoringService, SharedModelFeatureCache
from trader.recommendation.application.ports.loaded_profile import (
    HeadRuntime,
    LoadedScoringProfile,
    ProfileEvidence,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget
from trader.recommendation.domain.selection.execution_cost import ExecutionCostPolicy
from trader.training.infra.model_bundles.composition import SingleHeadCombiner


def BuildUtilityScorer(
    profile: LoadedScoringProfile,
    strategy: Strategy,
    *,
    cost_policy: ExecutionCostPolicy,
    monotonic: Callable[[], float] = time.perf_counter,
    shared_features: SharedModelFeatureCache | None = None,
    prediction_target: PredictionTarget | None = None,
) -> ProductionModelScoringService:
    return ProductionModelScoringService(
        profile, strategy, plan={"v2": V2_D25_PLAN, "v3": V3_D25_PLAN, "v5": V5_D25_PLAN}[profile.profile_id],
        cost_policy=cost_policy, monotonic=monotonic, shared_features=shared_features, prediction_target=prediction_target,
    )


def profile_for(
    predictor,  # noqa: ANN001 - structural test double
    strategy: Strategy = Strategy.TOMORROW,
) -> LoadedScoringProfile:
    return LoadedScoringProfile(
        profile_id=predictor.profile_id,
        heads={
            strategy: HeadRuntime(
                strategy,
                predictor,
                SingleHeadCombiner(),
                ProfileEvidence("historical_unavailable", (), "manual_user_override"),
            )
        },
    )
