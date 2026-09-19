"""Risk-fact evaluation boundary for recommendation decisions."""

from __future__ import annotations

from typing import Protocol

from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.risk.downside import DownsideAssessment, assess_downside


class RiskControlPort(Protocol):
    """Calculate point-in-time downside facts without publishing decisions."""

    def assess(self, features: FeatureSnapshot, strategy: Strategy) -> DownsideAssessment: ...


class RiskControlService(RiskControlPort):
    """Stateless adapter around the domain downside rules."""

    def assess(self, features: FeatureSnapshot, strategy: Strategy) -> DownsideAssessment:
        return assess_downside(features, strategy)


__all__ = ["RiskControlPort", "RiskControlService"]
