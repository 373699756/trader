"""Assemble the V1 production scoring profile."""

from __future__ import annotations

from trader.application.ports.model_scoring import ProfileEvidence
from trader.infra.scoring.profiles.v1.artifact_codec import V1TomorrowModelArtifact
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


__all__ = ["build_v1_tomorrow_predictor"]
