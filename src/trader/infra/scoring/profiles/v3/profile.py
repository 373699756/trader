"""Assemble the V3 production scoring profile."""

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
from trader.infra.scoring.profiles.v3.bundle_codec import V3TomorrowBundleArtifact
from trader.infra.scoring.profiles.v3.composition import SingleHeadCombiner
from trader.infra.scoring.profiles.v3.heads.tomorrow.predictor import V3TomorrowPredictor


def build_tomorrow_predictor(artifact: V3TomorrowBundleArtifact) -> V3TomorrowPredictor:
    try:
        return V3TomorrowPredictor(artifact, _evidence(artifact))
    except LightGBMError as exc:
        raise ValueError("Tomorrow V3 LightGBM model is invalid") from exc


def build_scoring_profile(artifact: V3TomorrowBundleArtifact) -> LoadedScoringProfile:
    predictor = build_tomorrow_predictor(artifact)
    evidence = _evidence(artifact)
    return LoadedScoringProfile(
        profile_id="v3",
        heads={
            Strategy.TOMORROW: HeadRuntime(
                Strategy.TOMORROW,
                cast(HeadPredictorPort, predictor),
                SingleHeadCombiner(),
                evidence,
            )
        },
    )


def _evidence(artifact: V3TomorrowBundleArtifact) -> ProfileEvidence:
    return ProfileEvidence(
        artifact.historical_status,
        artifact.historical_failure_reasons,
        "manual_user_override",
        training_anchor=artifact.training_anchor,
        runtime_anchor=artifact.runtime_anchor,
        point_in_time_parity=artifact.point_in_time_parity,
    )


__all__ = ["build_scoring_profile", "build_tomorrow_predictor"]
