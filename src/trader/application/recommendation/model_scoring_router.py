"""Route model scoring by strategy while keeping profile details out of callers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType

from trader.application.ports.model_scoring import (
    ModelScoreBatch,
    ModelScoringContext,
    ModelScoringPort,
    ScoringCapabilityPort,
    ScoringProfileRuntimeStatus,
)
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.domain.publication.models import Strategy


class ModelScoringRouter(ModelScoringPort):
    """Expose one scoring capability for Tomorrow and D25."""

    _SUPPORTED_STRATEGIES = frozenset({Strategy.TOMORROW, Strategy.D25})

    def __init__(
        self,
        profile_id: ScoringProfileId,
        capabilities: Mapping[Strategy, ScoringCapabilityPort],
    ) -> None:
        values = dict(capabilities)
        if not values or any(strategy not in self._SUPPORTED_STRATEGIES for strategy in values):
            raise ValueError("model scoring capabilities are invalid")
        if any(capability.status().profile_id != profile_id for capability in values.values()):
            raise ValueError("model scoring capability profile identity is inconsistent")
        self._profile_id = profile_id
        self._capabilities = MappingProxyType(values)

    def uses_model(self, strategy: Strategy) -> bool:
        self._validate_strategy(strategy)
        return strategy in self._capabilities

    def history_required_sessions(self, strategy: Strategy) -> int:
        capability = self._capabilities.get(strategy)
        return capability.history_required_sessions if capability is not None else 20

    def is_input_eligible(self, strategy: Strategy, feature: FeatureSnapshot) -> bool:
        capability = self._capabilities.get(strategy)
        return capability.is_input_eligible(feature) if capability is not None else True

    def score(
        self,
        strategy: Strategy,
        features: Sequence[FeatureSnapshot],
        *,
        context: ModelScoringContext | None = None,
    ) -> ModelScoreBatch | None:
        capability = self._capabilities.get(strategy)
        return capability.score(features, context=context) if capability is not None else None

    def status(self) -> ScoringProfileRuntimeStatus | None:
        return ScoringProfileRuntimeStatus(
            self._profile_id,
            {strategy: capability.status() for strategy, capability in self._capabilities.items()},
        )

    @staticmethod
    def _validate_strategy(strategy: Strategy) -> None:
        if strategy not in ModelScoringRouter._SUPPORTED_STRATEGIES:
            raise ValueError(f"{strategy.value} strategy does not use the scoring router")


__all__ = ["ModelScoringRouter"]
