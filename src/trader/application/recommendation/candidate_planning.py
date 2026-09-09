"""Strategy-owned candidate qualification, ranking, and bounded reserve planning."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType

from trader.application.ports.model_scoring import ModelScoringPort
from trader.application.recommendation.policy import RecommendationPolicy
from trader.application.recommendation.scored_quality import has_transient_evaluation_gap
from trader.application.recommendation.scored_selection import (
    ScoredSelectionIdentity,
    ScoredSelectionOptions,
    plan_scored_feature_candidates,
)
from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.models import Strategy
from trader.domain.recommendation.selection.scored_selection import ScoredCandidatePlan

SCORED_STRATEGIES = (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)


@dataclass(frozen=True)
class CandidatePlanSet:
    plans: Mapping[Strategy, ScoredCandidatePlan]
    limit_per_board: int

    def __post_init__(self) -> None:
        plans = dict(self.plans)
        if set(plans) != set(SCORED_STRATEGIES):
            raise ValueError("candidate planning requires every scored strategy")
        if not isinstance(self.limit_per_board, int) or isinstance(self.limit_per_board, bool):
            raise ValueError("candidate planning board limit must be an integer")
        if not 1 <= self.limit_per_board <= 120:
            raise ValueError("candidate planning board limit must be between 1 and 120")
        object.__setattr__(self, "plans", MappingProxyType(plans))

    def strategy_codes(self, strategy: Strategy) -> tuple[str, ...]:
        return self.plans[strategy].limited_codes(self.limit_per_board)

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

    def __post_init__(self) -> None:
        object.__setattr__(self, "features", tuple(self.features))
        strategies = frozenset(self.transient_invalid_strategies)
        if not strategies.issubset(SCORED_STRATEGIES):
            raise ValueError("candidate refresh transient strategies must be scored strategies")
        object.__setattr__(self, "transient_invalid_strategies", strategies)


@dataclass(frozen=True)
class CandidatePlanningContext:
    evaluated_at: datetime
    data_version: str
    policy: RecommendationPolicy
    model_scoring: ModelScoringPort | None
    limit_per_board: int


def build_candidate_plans(
    population: Sequence[FeatureSnapshot],
    candidate_features: Sequence[FeatureSnapshot] | None,
    context: CandidatePlanningContext,
) -> CandidatePlanSet:
    features = tuple(population)
    candidates = tuple(candidate_features) if candidate_features is not None else None
    plans = {
        strategy: plan_scored_feature_candidates(
            features,
            context.policy,
            ScoredSelectionOptions(
                evaluated_at=context.evaluated_at,
                max_age_seconds=_maximum_age_seconds(strategy),
                population_evaluated_at=context.evaluated_at,
                population_max_age_seconds=_maximum_age_seconds(strategy),
                phase="candidate_discovery",
                candidate_features=candidates,
                normalize_discovery_source_time=True,
                strategy=strategy,
                minimum_history_sessions=_history_required_sessions(context.model_scoring, strategy),
                model_input_eligible_codes=_model_eligible_codes(
                    context.model_scoring,
                    strategy,
                    candidates if candidates is not None else features,
                ),
                candidate_limit_per_board=context.limit_per_board,
            ),
            ScoredSelectionIdentity(
                trade_date=context.evaluated_at.date(),
                data_version=context.data_version,
                merge_epoch=context.data_version,
            ),
        )
        for strategy in SCORED_STRATEGIES
    }
    return CandidatePlanSet(plans, context.limit_per_board)


def _maximum_age_seconds(strategy: Strategy) -> float:
    return 20.0 if strategy is Strategy.TODAY else 30.0


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


def refresh_candidate_reserves(
    population: tuple[FeatureSnapshot, ...],
    initial_plans: CandidatePlanSet,
    context: CandidatePlanningContext,
    refresh_quotes: Callable[[tuple[str, ...]], Sequence[FeatureSnapshot]],
) -> CandidateRefreshPlan:
    pending = initial_plans.physical_union()
    if not pending:
        return CandidateRefreshPlan(
            (),
            build_candidate_plans(
                population,
                (),
                replace(
                    context,
                    evaluated_at=_planning_evaluated_at(context.evaluated_at, population),
                    limit_per_board=initial_plans.limit_per_board,
                ),
            ),
        )
    attempted: set[str] = set()
    refreshed: dict[str, FeatureSnapshot] = {}
    final_plans = initial_plans
    transient_invalid_strategies: set[Strategy] = set()
    while pending:
        requested = tuple(pending)
        attempted.update(requested)
        wave = tuple(refresh_quotes(requested))
        requested_codes = set(requested)
        wave_by_code = {feature.quote.code: feature for feature in wave if feature.quote.code in requested_codes}
        missing_codes = requested_codes - set(wave_by_code)
        transient_invalid_strategies.update(initial_plans.strategies_for_codes(missing_codes))
        if wave_by_code:
            refreshed.update(wave_by_code)
        final_plans = build_candidate_plans(
            population,
            tuple(refreshed.values()),
            replace(
                context,
                evaluated_at=_planning_evaluated_at(
                    context.evaluated_at,
                    population,
                    tuple(refreshed.values()),
                ),
                limit_per_board=initial_plans.limit_per_board,
            ),
        )
        transient_invalid_strategies.update(_transient_refresh_strategies(final_plans, requested))
        pending = _replacement_codes(initial_plans, final_plans, attempted)
    return CandidateRefreshPlan(tuple(refreshed.values()), final_plans, frozenset(transient_invalid_strategies))


def _transient_refresh_strategies(
    plan: CandidatePlanSet,
    requested: tuple[str, ...],
) -> frozenset[Strategy]:
    requested_codes = set(requested)
    return frozenset(
        strategy
        for strategy in SCORED_STRATEGIES
        if any(
            has_transient_evaluation_gap(item)
            for item in plan.plans[strategy].evaluations
            if item.code in requested_codes
        )
    )


def _replacement_codes(
    initial: CandidatePlanSet,
    current: CandidatePlanSet,
    attempted: set[str],
) -> tuple[str, ...]:
    replacements: list[str] = []
    for strategy in SCORED_STRATEGIES:
        for board in initial.plans[strategy].reserves:
            initial_reserve = initial.plans[strategy].reserves[board]
            current_reserve = current.plans[strategy].reserves.get(board, ())
            target = min(initial.limit_per_board, len(initial_reserve))
            missing = max(0, target - min(current.limit_per_board, len(current_reserve)))
            replacements.extend(tuple(code for code in initial_reserve if code not in attempted)[:missing])
    return tuple(dict.fromkeys(replacements))


def _planning_evaluated_at(
    observed_at: datetime,
    *feature_groups: tuple[FeatureSnapshot, ...],
) -> datetime:
    values = [observed_at]
    for features in feature_groups:
        for feature in features:
            values.extend((feature.observed_at, feature.quote.source_time, feature.quote.received_time))
    return max(value.astimezone(observed_at.tzinfo) for value in values)


__all__ = [
    "CandidatePlanSet",
    "CandidatePlanningContext",
    "CandidateRefreshPlan",
    "SCORED_STRATEGIES",
    "build_candidate_plans",
    "refresh_candidate_reserves",
]
