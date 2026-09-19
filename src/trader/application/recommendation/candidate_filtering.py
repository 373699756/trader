"""Candidate qualification boundary for the production recommendation chain.

The scheduler owns refresh timing, while this module owns the candidate
qualification contract.  It deliberately returns immutable plans and never
mutates the market reader, model bundle, or training artifacts.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from trader.application.ports.model_scoring import ModelScoringPort
from trader.application.recommendation.candidate_planning import (
    CandidatePlanningContext,
    CandidatePlanSet,
    CandidateRefreshPlan,
    build_candidate_plans,
    refresh_candidate_reserves,
)
from trader.application.recommendation.policy import RecommendationPolicy
from trader.recommendation.domain.market.models import FeatureSnapshot


class CandidateFilteringPort(Protocol):
    """Qualification and bounded reserve operations used by market refresh."""

    def plan(
        self,
        population: Sequence[FeatureSnapshot],
        candidate_features: Sequence[FeatureSnapshot] | None,
        *,
        evaluated_at: datetime,
        data_version: str,
        apply_model_eligibility: bool = True,
    ) -> CandidatePlanSet: ...

    def refresh(  # noqa: PLR0913 - explicit refresh port includes deadline and refill capabilities
        self,
        population: tuple[FeatureSnapshot, ...],
        initial_plans: CandidatePlanSet,
        *,
        evaluated_at: datetime,
        data_version: str,
        refresh_quotes: Callable[[tuple[str, ...]], Sequence[FeatureSnapshot]],
        can_refill: Callable[[], bool],
    ) -> CandidateRefreshPlan: ...


@dataclass(frozen=True)
class CandidateFilteringService(CandidateFilteringPort):
    """Build and refill strategy-owned candidate reserves.

    The service is stateless.  A new call receives all observations it needs,
    which makes candidate qualification independently testable and prevents a
    later training run from sharing mutable state with live recommendations.
    """

    policy: RecommendationPolicy
    model_scoring: ModelScoringPort | None
    limit_per_board: int

    def __post_init__(self) -> None:
        if self.limit_per_board < 1:
            raise ValueError("candidate filtering board limit must be positive")

    def plan(
        self,
        population: Sequence[FeatureSnapshot],
        candidate_features: Sequence[FeatureSnapshot] | None,
        *,
        evaluated_at: datetime,
        data_version: str,
        apply_model_eligibility: bool = True,
    ) -> CandidatePlanSet:
        return build_candidate_plans(
            population,
            candidate_features,
            CandidatePlanningContext(
                evaluated_at=evaluated_at,
                data_version=data_version,
                policy=self.policy,
                model_scoring=self.model_scoring,
                limit_per_board=self.limit_per_board,
                apply_model_eligibility=apply_model_eligibility,
            ),
        )

    def refresh(  # noqa: PLR0913 - explicit refresh port implementation mirrors the stable port contract
        self,
        population: tuple[FeatureSnapshot, ...],
        initial_plans: CandidatePlanSet,
        *,
        evaluated_at: datetime,
        data_version: str,
        refresh_quotes: Callable[[tuple[str, ...]], Sequence[FeatureSnapshot]],
        can_refill: Callable[[], bool],
    ) -> CandidateRefreshPlan:
        return refresh_candidate_reserves(
            population,
            initial_plans,
            CandidatePlanningContext(
                evaluated_at=evaluated_at,
                data_version=data_version,
                policy=self.policy,
                model_scoring=self.model_scoring,
                limit_per_board=initial_plans.limit_per_board,
            ),
            refresh_quotes,
            can_refill=can_refill,
        )


__all__ = ["CandidateFilteringPort", "CandidateFilteringService"]
