"""Published-model scoring boundary used by live recommendation runs.

This module accepts a model-scoring port only.  Training, artifact fitting,
history downloading, and model publication remain outside the application
recommendation package.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from trader.application.ports.model_scoring import (
    ModelScoreBatch,
    ModelScoringContext,
    ModelScoringPort,
    ScoringProfileRuntimeStatus,
)
from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.models import Strategy


@dataclass(frozen=True)
class PublishedModelScoringService:
    """Delegate live scoring to already-loaded, immutable model capabilities."""

    capability: ModelScoringPort

    def uses_model(self, strategy: Strategy) -> bool:
        """Report whether a published head is available for ``strategy``."""

        return self.capability.uses_model(strategy)

    def history_required_sessions(self, strategy: Strategy) -> int:
        """Return the history window required by the published head."""

        return self.capability.history_required_sessions(strategy)

    def is_input_eligible(self, strategy: Strategy, feature: FeatureSnapshot) -> bool:
        """Apply the published head's input contract without fitting a model."""

        return self.capability.is_input_eligible(strategy, feature)

    def score(
        self,
        strategy: Strategy,
        features: Sequence[FeatureSnapshot],
        *,
        context: ModelScoringContext | None = None,
    ) -> ModelScoreBatch | None:
        return self.capability.score(strategy, features, context=context)

    def status(self) -> ScoringProfileRuntimeStatus | None:
        return self.capability.status()


__all__ = ["PublishedModelScoringService"]
