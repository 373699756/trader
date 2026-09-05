"""Assemble the V2 production scoring profile."""

from __future__ import annotations

from trader.application.ports.model_scoring import ProfileEvidence
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


def build_v2_tomorrow_predictor(artifact: V2TomorrowModelArtifact) -> V2TomorrowPredictor:
    return V2TomorrowPredictor(artifact, _EVIDENCE)


__all__ = ["build_v2_tomorrow_predictor"]
