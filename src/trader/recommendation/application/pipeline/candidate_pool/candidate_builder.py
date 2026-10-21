"""Strategy-owned complete scoring-input qualification."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from trader.recommendation.application.pipeline.dynamic_filter.filter_executor import (
    PlanScoredFeatureCandidates,
    ScoredSelectionIdentity,
    ScoredSelectionOptions,
)
from trader.recommendation.application.pipeline.dynamic_standardize.dynamic_normalization import (
    NormalizeCandidateDiscoveryPopulation,
)
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.application.ports.loaded_profile import ModelScoringPort
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.selection.scored_selection import ScoredCandidatePlan

SCORED_STRATEGIES = (Strategy.TOMORROW, Strategy.D25)


@dataclass(frozen=True)
class CandidatePlanSet:
    plans: Mapping[Strategy, ScoredCandidatePlan]
    population_evaluated_at: datetime

    def __post_init__(self) -> None:
        if self.population_evaluated_at.tzinfo is None or self.population_evaluated_at.utcoffset() is None:
            raise ValueError("candidate population time must be timezone-aware")
        plans = dict(self.plans)
        if set(plans) != set(SCORED_STRATEGIES):
            raise ValueError("candidate planning requires every scored strategy")
        object.__setattr__(self, "plans", MappingProxyType(plans))

    def strategy_codes(self, strategy: Strategy) -> tuple[str, ...]:
        return self.plans[strategy].qualified_codes()

    def physical_union(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(code for strategy in SCORED_STRATEGIES for code in self.strategy_codes(strategy)))

    def strategies_for_codes(self, codes: Collection[str]) -> frozenset[Strategy]:
        requested = set(codes)
        return frozenset(
            strategy
            for strategy, plan in self.plans.items()
            if any(requested.intersection(board_codes) for board_codes in plan.reserves.values())
        )


@dataclass(frozen=True)
class CandidateRefreshPlan:
    features: tuple[FeatureSnapshot, ...]
    plans: CandidatePlanSet
    transient_invalid_strategies: frozenset[Strategy] = frozenset()
    deadline_reached: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "features", tuple(self.features))
        strategies = frozenset(self.transient_invalid_strategies)
        if not strategies.issubset(SCORED_STRATEGIES):
            raise ValueError("candidate refresh transient strategies must be scored strategies")
        if not isinstance(self.deadline_reached, bool):
            raise ValueError("candidate refresh deadline state must be boolean")
        object.__setattr__(self, "transient_invalid_strategies", strategies)


@dataclass(frozen=True)
class CandidatePlanningContext:
    evaluated_at: datetime
    data_version: str
    policy: RecommendationPolicy
    model_scoring: ModelScoringPort | None
    apply_model_eligibility: bool = True
    population_evaluated_at: datetime | None = None


def BuildCandidatePlans(
    population: Sequence[FeatureSnapshot],
    candidate_features: Sequence[FeatureSnapshot] | None,
    context: CandidatePlanningContext,
) -> CandidatePlanSet:
    population_at = context.population_evaluated_at or context.evaluated_at
    if population_at.date() != context.evaluated_at.date():
        raise ValueError("candidate_population_trade_date_mismatch")
    features = NormalizeCandidateDiscoveryPopulation(population, population_at)
    candidates = tuple(candidate_features) if candidate_features is not None else None
    plans = {
        strategy: PlanScoredFeatureCandidates(
            features,
            context.policy,
            ScoredSelectionOptions(
                evaluated_at=context.evaluated_at,
                max_age_seconds=_maximum_age_seconds(strategy),
                population_evaluated_at=population_at,
                population_max_age_seconds=_maximum_age_seconds(strategy),
                phase="candidate_discovery",
                candidate_features=candidates,
                normalize_discovery_source_time=False,
                strategy=strategy,
                minimum_history_sessions=_history_required_sessions(
                    context.model_scoring if context.apply_model_eligibility else None,
                    strategy,
                ),
                model_input_eligible_codes=_model_eligible_codes(
                    context.model_scoring if context.apply_model_eligibility else None,
                    strategy,
                    candidates if candidates is not None else features,
                ),
            ),
            ScoredSelectionIdentity(
                trade_date=context.evaluated_at.date(),
                data_version=context.data_version,
                merge_epoch=context.data_version,
            ),
        )
        for strategy in SCORED_STRATEGIES
    }
    return CandidatePlanSet(plans, population_at)


def _maximum_age_seconds(strategy: Strategy) -> float:
    del strategy
    return 30.0


def _history_required_sessions(model_scoring: ModelScoringPort | None, strategy: Strategy) -> int:
    return model_scoring.history_required_sessions(strategy) if model_scoring is not None else 20


def _model_eligible_codes(
    model_scoring: ModelScoringPort | None,
    strategy: Strategy,
    features: Sequence[FeatureSnapshot],
) -> frozenset[str] | None:
    if model_scoring is None or not model_scoring.uses_model(strategy):
        return None
    return frozenset(feature.quote.code for feature in features if model_scoring.is_input_eligible(strategy, feature))


def RefreshQualifiedInputs(
    population: tuple[FeatureSnapshot, ...],
    initial_plans: CandidatePlanSet,
    context: CandidatePlanningContext,
    refresh_quotes: Callable[[tuple[str, ...]], Sequence[FeatureSnapshot]],
    *,
    can_refresh: Callable[[], bool],
) -> CandidateRefreshPlan:
    if initial_plans.population_evaluated_at.date() != context.evaluated_at.date():
        raise ValueError("candidate_population_trade_date_mismatch")
    requested = initial_plans.physical_union()
    if requested and not can_refresh():
        return CandidateRefreshPlan((), initial_plans, deadline_reached=True)
    requested_codes = frozenset(requested)
    features = (
        tuple(item for item in refresh_quotes(requested) if item.quote.code in requested_codes) if requested else ()
    )
    missing = requested_codes - {item.quote.code for item in features}
    # Preserve the complete requested population. Missing observations belong to
    # the quality boundary and never trigger a reserve or a replacement request.
    return CandidateRefreshPlan(
        features,
        initial_plans,
        initial_plans.strategies_for_codes(missing),
        deadline_reached=bool(requested and not can_refresh()),
    )


__all__ = [
    "CandidatePlanSet",
    "CandidatePlanningContext",
    "CandidateRefreshPlan",
    "SCORED_STRATEGIES",
    "BuildCandidatePlans",
    "RefreshQualifiedInputs",
]
