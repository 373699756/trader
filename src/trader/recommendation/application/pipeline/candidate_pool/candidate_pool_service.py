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

from trader.recommendation.application.pipeline.candidate_pool.candidate_builder import (
    BuildCandidatePlans,
    CandidatePlanningContext,
    CandidatePlanSet,
    CandidateRefreshPlan,
    RefreshQualifiedInputs,
)
from trader.recommendation.application.pipeline.candidate_pool.candidate_pipeline import (
    CandidatePipelineResult,
    ExecuteCandidatePipeline,
)
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.application.pipeline.stage_output import PipelineStageOutput
from trader.recommendation.application.ports.loaded_profile import ModelScoringPort
from trader.recommendation.domain.market.models import FeatureSnapshot


class CandidateFilteringPort(Protocol):
    """Qualification and complete input operations used by market refresh."""

    def plan_observed(
        self,
        source: PipelineStageOutput[FeatureSnapshot],
        *,
        evaluated_at: datetime,
        data_version: str,
        monotonic: Callable[[], float],
        apply_model_eligibility: bool = True,
    ) -> CandidatePipelineResult: ...

    def plan(
        self,
        population: Sequence[FeatureSnapshot],
        candidate_features: Sequence[FeatureSnapshot] | None,
        *,
        evaluated_at: datetime,
        data_version: str,
        apply_model_eligibility: bool = True,
    ) -> CandidatePlanSet: ...

    def refresh(  # noqa: PLR0913 - explicit refresh port includes deadline and refresh capabilities
        self,
        population: tuple[FeatureSnapshot, ...],
        initial_plans: CandidatePlanSet,
        *,
        evaluated_at: datetime,
        data_version: str,
        refresh_quotes: Callable[[tuple[str, ...]], Sequence[FeatureSnapshot]],
        can_refresh: Callable[[], bool],
    ) -> CandidateRefreshPlan: ...


@dataclass(frozen=True)
class CandidateFilteringService(CandidateFilteringPort):
    """Prepare every qualified input for each scored strategy.

    The service is stateless.  A new call receives all observations it needs,
    which makes candidate qualification independently testable and prevents a
    later training run from sharing mutable state with live recommendations.
    """

    policy: RecommendationPolicy
    model_scoring: ModelScoringPort | None

    def plan_observed(
        self,
        source: PipelineStageOutput[FeatureSnapshot],
        *,
        evaluated_at: datetime,
        data_version: str,
        monotonic: Callable[[], float],
        apply_model_eligibility: bool = True,
    ) -> CandidatePipelineResult:
        return ExecuteCandidatePipeline(
            source,
            CandidatePlanningContext(
                evaluated_at,
                data_version,
                self.policy,
                self.model_scoring,
                apply_model_eligibility,
            ),
            monotonic,
        )

    def plan(
        self,
        population: Sequence[FeatureSnapshot],
        candidate_features: Sequence[FeatureSnapshot] | None,
        *,
        evaluated_at: datetime,
        data_version: str,
        apply_model_eligibility: bool = True,
    ) -> CandidatePlanSet:
        return BuildCandidatePlans(
            population,
            candidate_features,
            CandidatePlanningContext(
                evaluated_at=evaluated_at,
                data_version=data_version,
                policy=self.policy,
                model_scoring=self.model_scoring,
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
        can_refresh: Callable[[], bool],
    ) -> CandidateRefreshPlan:
        return RefreshQualifiedInputs(
            population,
            initial_plans,
            CandidatePlanningContext(
                evaluated_at=evaluated_at,
                data_version=data_version,
                policy=self.policy,
                model_scoring=self.model_scoring,
            ),
            refresh_quotes,
            can_refresh=can_refresh,
        )


__all__ = ["CandidateFilteringPort", "CandidateFilteringService"]
