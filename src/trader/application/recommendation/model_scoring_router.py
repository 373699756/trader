"""Route model scoring by strategy while keeping profile details out of callers."""

from __future__ import annotations

from collections.abc import Sequence

from trader.application.ports.model_scoring import (
    ModelScoreBatch,
    ModelScoringPort,
    ScoringCapabilityPort,
    ScoringProfileRuntimeStatus,
)
from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.models import Strategy


class ModelScoringRouter(ModelScoringPort):
    """Expose one scoring capability for all three short-horizon strategies."""

    _SUPPORTED_STRATEGIES = frozenset({Strategy.TODAY, Strategy.TOMORROW, Strategy.D25})

    def __init__(self, tomorrow: ScoringCapabilityPort | None) -> None:
        self._tomorrow = tomorrow

    def uses_model(self, strategy: Strategy) -> bool:
        self._validate_strategy(strategy)
        return strategy is Strategy.TOMORROW and self._tomorrow is not None

    def history_required_sessions(self, strategy: Strategy) -> int:
        return self._tomorrow.history_required_sessions if self.uses_model(strategy) and self._tomorrow else 20

    def is_input_eligible(self, strategy: Strategy, feature: FeatureSnapshot) -> bool:
        return self._tomorrow.is_input_eligible(feature) if self.uses_model(strategy) and self._tomorrow else True

    def score(self, strategy: Strategy, features: Sequence[FeatureSnapshot]) -> ModelScoreBatch | None:
        return self._tomorrow.score(features) if self.uses_model(strategy) and self._tomorrow else None

    def status(self) -> ScoringProfileRuntimeStatus | None:
        return self._tomorrow.status() if self._tomorrow is not None else None

    @staticmethod
    def _validate_strategy(strategy: Strategy) -> None:
        if strategy not in ModelScoringRouter._SUPPORTED_STRATEGIES:
            raise ValueError(f"{strategy.value} strategy does not use the scoring router")


__all__ = ["ModelScoringRouter"]
