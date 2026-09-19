"""Read-only scored-strategy feature assembly and deterministic local selection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime

from trader.application.ports.market import DataPlaneReadPort, MarketDataPlaneSnapshot
from trader.application.recommendation.feature_calculation import (
    FeatureCalculationPort,
    FeatureCalculationService,
    ScoredSelectionNotReadyError,
    assemble_scored_features,
)
from trader.application.recommendation.policy import RecommendationPolicy
from trader.recommendation.domain.market.models import Board, FeatureSnapshot
from trader.recommendation.domain.publication.models import ScoredSelectionResult, Strategy
from trader.recommendation.domain.selection.ranking import minimum_selection_score
from trader.recommendation.domain.selection.scored_selection import (
    BoardCrossSectionFallback,
    ScoredCandidatePlan,
    ScoredSelectionPolicy,
    ScoredSelectionRequest,
    plan_scored_candidates,
    select_scored,
)

_SUPPORTED_BOARDS = (Board.MAIN, Board.CHINEXT, Board.STAR)


@dataclass(frozen=True)
class ScoredSelectionOptions:
    evaluated_at: datetime
    max_age_seconds: float
    phase: str = "tomorrow"
    fallbacks: Mapping[Board, BoardCrossSectionFallback] | None = None
    candidate_features: tuple[FeatureSnapshot, ...] | None = None
    normalize_discovery_source_time: bool = False
    strategy: Strategy = Strategy.TOMORROW
    population_evaluated_at: datetime | None = None
    population_max_age_seconds: float | None = None
    minimum_history_sessions: int = 20
    model_input_eligible_codes: frozenset[str] | None = None
    candidate_limit_per_board: int = 120


@dataclass(frozen=True)
class ScoredSelectionIdentity:
    trade_date: date
    data_version: str
    merge_epoch: str


class ScoredSelectionUseCase:
    def __init__(
        self,
        reader: DataPlaneReadPort,
        policy: RecommendationPolicy,
        feature_calculation: FeatureCalculationPort | None = None,
    ) -> None:
        self._reader = reader
        self._policy = policy
        self._feature_calculation = (
            feature_calculation if feature_calculation is not None else FeatureCalculationService()
        )

    def execute(
        self,
        *,
        evaluated_at: datetime,
        max_age_seconds: float,
        phase: str = "tomorrow",
        fallbacks: Mapping[Board, BoardCrossSectionFallback] | None = None,
    ) -> ScoredSelectionResult:
        snapshot = self._reader.snapshot()
        return select_scored_snapshot(
            snapshot,
            self._policy,
            ScoredSelectionOptions(
                evaluated_at=evaluated_at,
                max_age_seconds=max_age_seconds,
                phase=phase,
                fallbacks=fallbacks,
            ),
            feature_calculation=self._feature_calculation,
        )


def select_scored_snapshot(
    snapshot: MarketDataPlaneSnapshot,
    policy: RecommendationPolicy,
    options: ScoredSelectionOptions,
    *,
    feature_calculation: FeatureCalculationPort | None = None,
) -> ScoredSelectionResult:
    evaluated_at = options.evaluated_at
    if snapshot.daily_features is None or snapshot.market is None:
        raise ScoredSelectionNotReadyError("coherent_market_epoch_unavailable")
    if evaluated_at.date() != snapshot.market.trade_date:
        raise ScoredSelectionNotReadyError("market_epoch_trade_date_mismatch")
    epoch_times = [
        snapshot.daily_features.observed_at,
        snapshot.daily_features.received_at,
        snapshot.market.observed_at,
        snapshot.market.received_at,
    ]
    if snapshot.candidate_quotes is not None:
        epoch_times.extend(
            (
                snapshot.candidate_quotes.observed_at,
                snapshot.candidate_quotes.received_at,
            )
        )
    if snapshot.research is not None:
        epoch_times.extend((snapshot.research.observed_at, snapshot.research.received_at))
    if max(epoch_times) > evaluated_at:
        raise ScoredSelectionNotReadyError("market_epoch_from_future")
    calculator = feature_calculation if feature_calculation is not None else FeatureCalculationService()
    features = calculator.calculate(snapshot)
    merge_epochs = {feature.merge_epoch for feature in features}
    if len(merge_epochs) != 1:
        raise ScoredSelectionNotReadyError("feature_merge_epoch_mismatch")
    return select_scored_features(
        features,
        policy,
        options,
        ScoredSelectionIdentity(
            trade_date=snapshot.market.trade_date,
            data_version=snapshot.market.content_hash,
            merge_epoch=next(iter(merge_epochs)),
        ),
    )


def select_scored_features(
    features: Sequence[FeatureSnapshot],
    policy: RecommendationPolicy,
    options: ScoredSelectionOptions,
    identity: ScoredSelectionIdentity,
    *,
    execution_gate_reasons: Mapping[str, str] | None = None,
) -> ScoredSelectionResult:
    """Select scored candidates from an already coherent point-in-time population."""

    return select_scored(
        _selection_request(
            features,
            policy,
            options,
            identity,
            execution_gate_reasons=execution_gate_reasons,
        )
    )


def plan_scored_feature_candidates(
    features: Sequence[FeatureSnapshot],
    policy: RecommendationPolicy,
    options: ScoredSelectionOptions,
    identity: ScoredSelectionIdentity,
) -> ScoredCandidatePlan:
    """Build the strategy-owned eligible reserve before any board cap is applied."""

    return plan_scored_candidates(_selection_request(features, policy, options, identity))


def normalize_candidate_discovery_population(
    features: Sequence[FeatureSnapshot],
    evaluated_at: datetime,
) -> tuple[FeatureSnapshot, ...]:
    """Normalize one discovery population once before strategy-specific planning."""

    normalized: list[FeatureSnapshot] = []
    for feature in features:
        source_time = min(evaluated_at, feature.quote.received_time)
        normalized.append(
            feature
            if feature.quote.source_time == source_time
            else replace(feature, quote=replace(feature.quote, source_time=source_time))
        )
    return tuple(normalized)


def _selection_request(
    features: Sequence[FeatureSnapshot],
    policy: RecommendationPolicy,
    options: ScoredSelectionOptions,
    identity: ScoredSelectionIdentity,
    *,
    execution_gate_reasons: Mapping[str, str] | None = None,
) -> ScoredSelectionRequest:
    evaluated_at = options.evaluated_at
    population = tuple(features)
    if evaluated_at.date() != identity.trade_date:
        raise ScoredSelectionNotReadyError("market_epoch_trade_date_mismatch")
    if not population:
        raise ScoredSelectionNotReadyError("coherent_market_epoch_unavailable")
    population_evaluated_at = options.population_evaluated_at or evaluated_at
    if options.normalize_discovery_source_time:
        population = normalize_candidate_discovery_population(population, population_evaluated_at)
    return ScoredSelectionRequest(
        features=population,
        evaluated_at=evaluated_at,
        trade_date=identity.trade_date.isoformat(),
        phase=options.phase,
        data_version=identity.data_version,
        merge_epoch=identity.merge_epoch,
        policy=_selection_policy(policy, options),
        candidate_features=options.candidate_features,
        fallbacks=options.fallbacks or {},
        execution_gate_reasons=execution_gate_reasons or {},
        population_evaluated_at=population_evaluated_at,
        population_max_age_seconds=options.population_max_age_seconds,
        minimum_history_sessions=options.minimum_history_sessions,
        model_input_eligible_codes=options.model_input_eligible_codes,
    )


def _selection_policy(
    policy: RecommendationPolicy,
    options: ScoredSelectionOptions,
) -> ScoredSelectionPolicy:
    board_policies = {
        board: board_policy
        for board in _SUPPORTED_BOARDS
        if (board_policy := policy.board_policy(options.strategy, board)) is not None
    }
    minimum_score = minimum_selection_score(
        options.strategy,
        policy.selection.thresholds,
        phase=options.phase,
        observation_margin=policy.selection.observation_margin,
    )
    return ScoredSelectionPolicy(
        board_policies=board_policies,
        risk_rules=policy.risk_rules,
        max_age_seconds=options.max_age_seconds,
        local_risk_cap=policy.fusion.local_risk_cap,
        candidate_limit_per_board=options.candidate_limit_per_board,
        top_k=min(policy.selection.default_top_k, 10),
        maximum_per_industry=policy.selection.maximum_per_industry,
        minimum_local_score=minimum_score if minimum_score is not None else 100.0,
        hard_filter=policy.hard_filter,
        strategy=options.strategy,
    )


__all__ = [
    "ScoredSelectionNotReadyError",
    "ScoredSelectionOptions",
    "ScoredSelectionUseCase",
    "assemble_scored_features",
    "normalize_candidate_discovery_population",
    "plan_scored_feature_candidates",
    "select_scored_features",
    "select_scored_snapshot",
]
