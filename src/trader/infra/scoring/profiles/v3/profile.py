"""Assemble the V3 production scoring profile."""

from __future__ import annotations

from typing import cast

from lightgbm.basic import LightGBMError

from trader.application.ports.model_scoring import (
    HeadPredictorPort,
    HeadRuntime,
    LoadedScoringProfile,
    ProfileEvidence,
    ProfileIdentity,
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
        identity=ProfileIdentity("v3", predictor.model_id, predictor.model_hash),
        heads=(HeadRuntime(Strategy.TOMORROW, cast(HeadPredictorPort, predictor)),),
        combiner=SingleHeadCombiner(),
        evidence=evidence,
    )


def _evidence(artifact: V3TomorrowBundleArtifact) -> ProfileEvidence:
    if artifact.training_input_scope == "partial_checkpoint":
        return ProfileEvidence(
            "historical_unavailable",
            ("partial_history_pipeline_trial",),
            "manual_user_override",
        )
    return ProfileEvidence("historical_validated", (), "trained_artifact")


__all__ = ["build_scoring_profile", "build_tomorrow_predictor"]
