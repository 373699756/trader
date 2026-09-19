"""Assemble one runtime profile over the two scored strategy heads."""

from __future__ import annotations

from typing import cast

from lightgbm.basic import LightGBMError

from trader.application.ports.model_scoring import (
    HeadPredictorPort,
    HeadRuntime,
    LoadedScoringProfile,
    ProfileEvidence,
)
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.domain.publication.models import Strategy
from trader.infra.scoring.composition import SingleHeadCombiner
from trader.training.infra.artifacts.bundle_codec import TrainedHeadBundleArtifact
from trader.training.infra.artifacts.predictor import TrainedHeadPredictor


def build_trained_scoring_profile(
    profile_id: ScoringProfileId,
    artifacts: tuple[TrainedHeadBundleArtifact, ...],
) -> LoadedScoringProfile:
    if profile_id not in {"v2", "v3"}:
        raise ValueError("shared trained heads require the V2 or V3 scoring profile")
    by_strategy = {artifact.strategy: artifact for artifact in artifacts}
    if len(artifacts) != 2 or set(by_strategy) != {Strategy.TOMORROW, Strategy.D25}:
        raise ValueError("shared scoring profiles require one Tomorrow and D25 bundle")
    try:
        predictors: dict[Strategy, HeadPredictorPort] = {
            strategy: cast(HeadPredictorPort, TrainedHeadPredictor(profile_id, by_strategy[strategy], strategy))
            for strategy in (Strategy.TOMORROW, Strategy.D25)
        }
    except LightGBMError as exc:
        raise ValueError("shared LightGBM model is invalid") from exc
    return LoadedScoringProfile(
        profile_id=profile_id,
        heads={
            strategy: HeadRuntime(strategy, predictor, SingleHeadCombiner(), _evidence(by_strategy[strategy]))
            for strategy, predictor in predictors.items()
        },
    )


def _evidence(artifact: TrainedHeadBundleArtifact) -> ProfileEvidence:
    return ProfileEvidence(
        artifact.historical_status,
        artifact.historical_failure_reasons,
        "manual_user_override",
        training_anchor=artifact.training_anchor,
        runtime_anchor=artifact.runtime_anchor,
        point_in_time_parity=artifact.point_in_time_parity,
    )


__all__ = ["build_trained_scoring_profile"]
