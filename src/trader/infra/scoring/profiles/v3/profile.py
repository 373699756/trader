"""Assemble the three independently identified V3 production heads."""

from __future__ import annotations

from typing import cast

from lightgbm.basic import LightGBMError

from trader.application.ports.model_scoring import (
    HeadPredictorPort,
    HeadRuntime,
    LoadedScoringProfile,
    ProfileEvidence,
)
from trader.domain.recommendation.models import Strategy
from trader.infra.scoring.profiles.v3.bundle_codec import V3HeadBundleArtifact
from trader.infra.scoring.profiles.v3.composition import SingleHeadCombiner
from trader.infra.scoring.profiles.v3.heads.d25.predictor import V3D25Predictor
from trader.infra.scoring.profiles.v3.heads.today.predictor import V3TodayPredictor
from trader.infra.scoring.profiles.v3.heads.tomorrow.predictor import V3TomorrowPredictor


def build_scoring_profile(
    artifacts: tuple[V3HeadBundleArtifact, ...],
) -> LoadedScoringProfile:
    by_strategy = {artifact.strategy: artifact for artifact in artifacts}
    if set(by_strategy) != {Strategy.TODAY, Strategy.TOMORROW, Strategy.D25}:
        raise ValueError("V3 requires one Today, Tomorrow, and D25 bundle")
    try:
        predictors: dict[Strategy, HeadPredictorPort] = {
            Strategy.TODAY: cast(HeadPredictorPort, V3TodayPredictor(by_strategy[Strategy.TODAY])),
            Strategy.TOMORROW: cast(HeadPredictorPort, V3TomorrowPredictor(by_strategy[Strategy.TOMORROW])),
            Strategy.D25: cast(HeadPredictorPort, V3D25Predictor(by_strategy[Strategy.D25])),
        }
    except LightGBMError as exc:
        raise ValueError("V3 LightGBM model is invalid") from exc
    return LoadedScoringProfile(
        profile_id="v3",
        heads={
            strategy: HeadRuntime(strategy, predictor, SingleHeadCombiner(), _evidence(by_strategy[strategy]))
            for strategy, predictor in predictors.items()
        },
    )


def _evidence(artifact: V3HeadBundleArtifact) -> ProfileEvidence:
    return ProfileEvidence(
        artifact.historical_status,
        artifact.historical_failure_reasons,
        "manual_user_override",
        training_anchor=artifact.training_anchor,
        runtime_anchor=artifact.runtime_anchor,
        point_in_time_parity=artifact.point_in_time_parity,
    )


__all__ = ["build_scoring_profile"]
