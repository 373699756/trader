"""Assemble the V1 production scoring profile."""

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
from trader.infra.scoring.profiles.v1.artifact_codec import V1TomorrowModelArtifact
from trader.infra.scoring.composition import SingleHeadCombiner
from trader.infra.scoring.profiles.v1.heads.tomorrow.predictor import V1TomorrowPredictor

_EVIDENCE = ProfileEvidence(
    historical_status="historical_unavailable",
    historical_failure_reasons=(
        "original_five_candidate_research_artifact_unavailable",
        "manual_daily_proxy_not_original_research_evidence",
    ),
    activation_basis="manual_user_override",
)


def build_v1_tomorrow_predictor(artifact: V1TomorrowModelArtifact) -> V1TomorrowPredictor:
    return V1TomorrowPredictor(artifact, _EVIDENCE)


def build_v1_scoring_profile(artifact: V1TomorrowModelArtifact) -> LoadedScoringProfile:
    predictor = build_v1_tomorrow_predictor(artifact)
    return LoadedScoringProfile(
        identity=ProfileIdentity("v1", predictor.model_id, predictor.model_hash),
        heads=(HeadRuntime(Strategy.TOMORROW, cast(HeadPredictorPort, predictor)),),
        combiner=SingleHeadCombiner(),
        evidence=_EVIDENCE,
    )


__all__ = ["build_v1_scoring_profile", "build_v1_tomorrow_predictor"]
