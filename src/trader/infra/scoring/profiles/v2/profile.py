"""Assemble the V2 production scoring profile."""

from __future__ import annotations

from typing import cast

from trader.application.ports.model_scoring import (
    HeadPredictorPort,
    HeadRuntime,
    LoadedScoringProfile,
    ProfileEvidence,
)
from trader.domain.recommendation.models import Strategy
from trader.infra.scoring.composition import SingleHeadCombiner
from trader.infra.scoring.profiles.v2.artifact_codec import V2TomorrowModelArtifact
from trader.infra.scoring.profiles.v2.heads.tomorrow.predictor import V2TomorrowPredictor

_EVIDENCE = ProfileEvidence(
    historical_status="historical_rejected",
    historical_failure_reasons=(
        "quintile_spread_not_positive",
        "severe_loss_rate_worse",
        "turnover_limit",
    ),
    activation_basis="manual_user_override",
)


def build_tomorrow_predictor(artifact: V2TomorrowModelArtifact) -> V2TomorrowPredictor:
    return V2TomorrowPredictor(artifact, _EVIDENCE)


def build_scoring_profile(artifact: V2TomorrowModelArtifact) -> LoadedScoringProfile:
    predictor = build_tomorrow_predictor(artifact)
    return LoadedScoringProfile(
        profile_id="v2",
        heads={
            Strategy.TOMORROW: HeadRuntime(
                Strategy.TOMORROW,
                cast(HeadPredictorPort, predictor),
                SingleHeadCombiner(),
                _EVIDENCE,
            )
        },
    )


__all__ = ["build_scoring_profile", "build_tomorrow_predictor"]
