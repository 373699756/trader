"""Assemble the V2 production scoring profile."""

from __future__ import annotations

from typing import cast

from trader.application.ports.model_scoring import (
    HeadPredictorPort,
    HeadRuntime,
    LoadedScoringProfile,
    ProfileEvidence,
    ProfileIdentity,
)
from trader.domain.recommendation.models import Strategy
from trader.infra.scoring.composition import SingleHeadCombiner
from trader.infra.scoring.profiles.v2.artifact_codec import TomorrowModelArtifact
from trader.infra.scoring.profiles.v2.heads.tomorrow.predictor import TomorrowPredictor

_EVIDENCE = ProfileEvidence(
    historical_status="historical_rejected",
    historical_failure_reasons=(
        "quintile_spread_not_positive",
        "severe_loss_rate_worse",
        "turnover_limit",
    ),
    activation_basis="manual_user_override",
)


def build_tomorrow_predictor(artifact: TomorrowModelArtifact) -> TomorrowPredictor:
    return TomorrowPredictor(artifact, _EVIDENCE)


def build_scoring_profile(artifact: TomorrowModelArtifact) -> LoadedScoringProfile:
    predictor = build_tomorrow_predictor(artifact)
    return LoadedScoringProfile(
        identity=ProfileIdentity("v2", predictor.model_id, predictor.model_hash),
        heads=(HeadRuntime(Strategy.TOMORROW, cast(HeadPredictorPort, predictor)),),
        combiner=SingleHeadCombiner(),
        evidence=_EVIDENCE,
    )


__all__ = ["build_scoring_profile", "build_tomorrow_predictor"]
