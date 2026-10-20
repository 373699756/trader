"""Explicitly unavailable model capability that keeps prediction admission closed."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from trader.recommendation.application.ports.loaded_profile import (
    ModelScoreBatch,
    ModelScoringContext,
    ScoringHeadRuntimeStatus,
)
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId


@dataclass(frozen=True)
class UnavailableModelCapability:
    """Require a model while rejecting inputs until a complete pair is installed."""

    profile_id: ScoringProfileId
    history_required_sessions: int
    reason: str = "production_model_missing"

    def is_input_eligible(self, feature: FeatureSnapshot) -> bool:
        return False

    def score(
        self,
        features: Sequence[FeatureSnapshot],
        *,
        context: ModelScoringContext | None = None,
    ) -> ModelScoreBatch:
        raise RuntimeError(self.reason)

    def status(self) -> ScoringHeadRuntimeStatus:
        return ScoringHeadRuntimeStatus(
            active=False,
            profile_id=self.profile_id,
            model_id="",
            model_hash="",
            scoring_version="",
            activation_basis="trained_artifact",
            historical_status="historical_unavailable",
            historical_failure_reasons=(self.reason,),
            monitoring_mode="automatic_t1_outcome_settlement",
            automatic_model_update=False,
            loss_probability_status="not_modeled",
        )


__all__ = ["UnavailableModelCapability"]
