"""Pure board-relative recommendation scoring."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from types import MappingProxyType
from typing import Literal

from trader.recommendation.domain.candidate.composition import Compose, LocalScoreResult
from trader.recommendation.domain.evidence.missing_value_policy import (
    AssessMissingValues,
    MissingFieldRule,
    MissingValuePolicy,
)
from trader.recommendation.domain.evidence.quality import MissingSeverity
from trader.recommendation.domain.market.factors import Clamp, WeightedScore
from trader.recommendation.domain.market.models import (
    Board,
    BoardPopulation,
    CrossSectionStats,
    FeatureSnapshot,
)
from trader.recommendation.domain.publication.models import (
    BoardStrategyPolicy,
    Strategy,
)
from trader.recommendation.domain.scoring.scoring_calculations import (
    MAX_FALLBACK_SESSIONS,
    MIN_BOARD_SAMPLE,
    SupportedWeight,
    _base_reliability,
    _default_competition_group,
    _distributions,
    _liquidity_bucket,
    _normalize_features,
    _population_version,
    _PopulationVersionIdentity,
    _quantile,
    _supported_weight,
    _value_or_missing,
)

BOARD_SCHEMA_VERSION = "board_cross_section_score_first"


@dataclass(frozen=True)
class BoardCrossSection:
    """Immutable board population and enriched feature values.

    ``reference_values`` is retained only for the bounded cache and contains
    primitive tuples, never mutable snapshots or external client objects.
    """

    board: Board
    merge_epoch: str
    trade_date: str
    phase: str
    data_version: str
    schema_version: str
    population: BoardPopulation
    features: tuple[FeatureSnapshot, ...]
    reference_values: Mapping[str, tuple[float, ...]]
    normalization: Mapping[str, CrossSectionStats]

    def __post_init__(self) -> None:
        object.__setattr__(self, "features", tuple(self.features))
        object.__setattr__(
            self,
            "reference_values",
            MappingProxyType({name: tuple(values) for name, values in self.reference_values.items()}),
        )
        object.__setattr__(self, "normalization", MappingProxyType(dict(self.normalization)))


@dataclass(frozen=True)
class BoardCrossSectionRequest:
    features: Sequence[FeatureSnapshot]
    board: Board
    merge_epoch: str
    trade_date: str
    phase: str
    data_version: str
    schema_version: str = BOARD_SCHEMA_VERSION
    fallback: BoardCrossSection | None = None
    fallback_age_sessions: int | None = None
    competition_groups: Mapping[str, tuple[str, str, str]] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "features", tuple(self.features))
        groups = None if self.competition_groups is None else MappingProxyType(dict(self.competition_groups))
        object.__setattr__(self, "competition_groups", groups)


@dataclass(frozen=True)
class _PopulationBasis:
    status: Literal["current", "fallback", "stale", "insufficient"]
    reference_values: Mapping[str, Sequence[float]]
    population_source: Mapping[str, Sequence[float]]
    fallback_date: str | None
    fallback_age: int | None


def BuildBoardCrossSection(request: BoardCrossSectionRequest) -> BoardCrossSection:
    """Build one isolated board cross-section without reading configuration or I/O."""

    _validate_request(request)
    ordered = tuple(
        sorted(
            (item for item in request.features if item.quote.board is request.board),
            key=lambda item: item.quote.code,
        )
    )
    current_distributions = _distributions(ordered)
    current_sample = sum(
        item.optional_value("amount_median_20d") is not None and item.value("amount_median_20d") > 0.0
        for item in ordered
    )
    basis = _population_basis(request, current_distributions, current_sample)
    population = _build_population(request, basis, current_sample, len(ordered))
    normalized, normalization = _normalize_features(
        ordered,
        basis.reference_values,
        population.population_version,
    )
    enriched = _enrich_features(normalized, population, request)
    return BoardCrossSection(
        board=request.board,
        merge_epoch=request.merge_epoch,
        trade_date=request.trade_date,
        phase=request.phase,
        data_version=request.data_version,
        schema_version=request.schema_version,
        population=population,
        features=enriched,
        reference_values={name: tuple(values) for name, values in basis.reference_values.items()},
        normalization=normalization,
    )


def _validate_request(request: BoardCrossSectionRequest) -> None:
    if request.board is Board.UNSUPPORTED:
        raise ValueError("unsupported board cannot be scored")
    identity = (request.merge_epoch, request.trade_date, request.phase, request.data_version, request.schema_version)
    if not all(identity):
        raise ValueError("board cross-section identity must not be empty")


def _population_basis(
    request: BoardCrossSectionRequest,
    current_distributions: Mapping[str, Sequence[float]],
    current_sample: int,
) -> _PopulationBasis:
    fallback = request.fallback
    fallback_age = request.fallback_age_sessions
    fallback_ok = (
        fallback is not None
        and fallback.board is request.board
        and fallback.phase == request.phase
        and fallback.population.status == "current"
        and fallback.population.sample_size >= MIN_BOARD_SAMPLE
        and fallback_age is not None
        and 0 <= fallback_age <= MAX_FALLBACK_SESSIONS
        and fallback.population.trade_date < request.trade_date
    )
    if current_sample >= MIN_BOARD_SAMPLE:
        return _PopulationBasis("current", current_distributions, current_distributions, None, None)
    if fallback_ok and fallback is not None:
        return _PopulationBasis(
            "fallback",
            fallback.reference_values,
            fallback.reference_values,
            fallback.population.trade_date,
            fallback_age,
        )
    if fallback is not None and fallback_age is not None and fallback_age > MAX_FALLBACK_SESSIONS:
        return _PopulationBasis(
            "stale",
            current_distributions,
            current_distributions,
            fallback.population.trade_date,
            fallback_age,
        )
    return _PopulationBasis("insufficient", current_distributions, current_distributions, None, None)


def _build_population(
    request: BoardCrossSectionRequest,
    basis: _PopulationBasis,
    current_sample: int,
    total_count: int,
) -> BoardPopulation:
    identity = _PopulationVersionIdentity(
        board=request.board,
        trade_date=request.trade_date,
        phase=request.phase,
        data_version=request.data_version,
        schema_version=request.schema_version,
        status=basis.status,
        fallback_date=basis.fallback_date,
        fallback_age=basis.fallback_age,
        merge_epoch=request.merge_epoch,
        reference_hash=hashlib.sha256(
            repr(tuple((name, tuple(values)) for name, values in sorted(basis.reference_values.items()))).encode()
        ).hexdigest(),
    )
    population_version = _population_version(identity)
    return BoardPopulation(
        trade_date=request.trade_date,
        phase=request.phase,
        board=request.board,
        data_version=request.data_version,
        schema_version=request.schema_version,
        population_version=population_version,
        sample_size=current_sample,
        missing_count=max(0, total_count - current_sample),
        liquidity_p50=_quantile(basis.population_source.get("amount_median_20d", ()), 0.50),
        liquidity_p80=_quantile(basis.population_source.get("amount_median_20d", ()), 0.80),
        fallback_trade_date=basis.fallback_date,
        fallback_age_sessions=basis.fallback_age,
        status=basis.status,
    )


def _enrich_features(
    normalized: Sequence[FeatureSnapshot],
    population: BoardPopulation,
    request: BoardCrossSectionRequest,
) -> tuple[FeatureSnapshot, ...]:
    enriched: list[FeatureSnapshot] = []
    for item in normalized:
        values = dict(item.values)
        reliability = _base_reliability(values)
        if population.status in {"stale", "insufficient"}:
            reliability = min(reliability, 0.84)
        group_id, group_source, group_version = (request.competition_groups or {}).get(
            item.quote.code,
            _default_competition_group(item),
        )
        missing_fields = tuple(sorted(name for name in values if _value_or_missing(values.get(name))))
        missing_reasons = {
            name: (
                "board cross-section sample is insufficient"
                if population.status in {"stale", "insufficient"} and name.endswith("_score")
                else "upstream value missing"
            )
            for name in missing_fields
        }
        enriched.append(
            replace(
                item,
                values=values,
                missing_fields=tuple(sorted(set(item.missing_fields).union(missing_fields))),
                missing_reasons={**dict(item.missing_reasons), **missing_reasons},
                board_data_reliability=reliability,
                board_population=population,
                merge_epoch=request.merge_epoch,
                competition_group_id=group_id,
                competition_group_source=group_source,
                competition_group_version=group_version,
                liquidity_bucket=_liquidity_bucket(
                    item.optional_value("amount_median_20d"),
                    population.liquidity_p50,
                    population.liquidity_p80,
                ),
                parameter_status=population.status,
            )
        )
    return tuple(enriched)


def ApplyBoardPolicy(
    cross_section: BoardCrossSection,
    strategy: Strategy,
    policy: BoardStrategyPolicy,
) -> tuple[FeatureSnapshot, ...]:
    if policy.strategy is not strategy or policy.board is not cross_section.board:
        raise ValueError("board policy does not match cross-section")
    result: list[FeatureSnapshot] = []
    for item in cross_section.features:
        supported = _supported_weight(
            strategy,
            item.values,
            policy.local_weights,
            phase=cross_section.phase,
        )
        reliability = supported
        if item.parameter_status in {"stale", "insufficient"}:
            reliability = min(reliability, 0.84)
        result.append(
            replace(
                item,
                board_data_reliability=reliability,
                board_supported_weight=supported,
                board_policy_id=policy.policy_id,
                board_policy_version=policy.version,
            )
        )
    return tuple(result)


def ProjectBoardPolicy(
    cross_section: BoardCrossSection,
    strategy: Strategy,
    policy: BoardStrategyPolicy,
    features: Sequence[FeatureSnapshot],
) -> tuple[FeatureSnapshot, ...]:
    """Project fresh inputs against the fixed board reference, regardless of batch size."""

    if policy.strategy is not strategy or policy.board is not cross_section.board:
        raise ValueError("board policy does not match cross-section")
    ordered = tuple(
        sorted(
            (item for item in features if item.quote.board is cross_section.board),
            key=lambda item: item.quote.code,
        )
    )
    normalized, _normalization = _normalize_features(
        ordered,
        cross_section.reference_values,
        cross_section.population.population_version,
    )
    request = BoardCrossSectionRequest(
        features=ordered,
        board=cross_section.board,
        merge_epoch=cross_section.merge_epoch,
        trade_date=cross_section.trade_date,
        phase=cross_section.phase,
        data_version=cross_section.data_version,
        schema_version=cross_section.schema_version,
    )
    enriched = _enrich_features(normalized, cross_section.population, request)
    return ApplyBoardPolicy(replace(cross_section, features=enriched), strategy, policy)


def EnrichBoardFeatures(
    strategy: Strategy,
    policy: BoardStrategyPolicy,
    request: BoardCrossSectionRequest,
) -> tuple[FeatureSnapshot, ...]:
    """Build and apply one board policy for callers without a cross-section cache."""

    if request.board is not policy.board:
        raise ValueError("board cross-section request does not match policy")
    cross_section = BuildBoardCrossSection(request)
    return ApplyBoardPolicy(cross_section, strategy, policy)


def ScoreBoardStrategy(snapshot: FeatureSnapshot, policy: BoardStrategyPolicy) -> LocalScoreResult:
    if snapshot.quote.board is not policy.board:
        raise ValueError("board score policy does not match snapshot board")
    if policy.strategy is Strategy.TOMORROW:
        components = {
            "tail_structure": WeightedScore(
                {
                    "tail_return_30m": snapshot.value("tail_return_30m"),
                    "tail_volume_ratio": snapshot.value("tail_volume_ratio"),
                    "close_location": snapshot.value("close_location"),
                },
                policy.local_component_weights["tail_structure"],
            ),
            "turnover_flow": WeightedScore(
                {
                    "turnover_shock_score": snapshot.value("turnover_shock_score"),
                    "amount_shock_score": snapshot.value("amount_shock_score"),
                    "flow_confirmation_score": snapshot.value("flow_confirmation_score"),
                },
                policy.local_component_weights["turnover_flow"],
            ),
            "trend": WeightedScore(
                {
                    "ma20_60_position": snapshot.value("ma20_60_position"),
                    "ma_slope": snapshot.value("ma_slope"),
                    "breakout_20d": snapshot.value("breakout_20d"),
                },
                policy.local_component_weights["trend"],
            ),
            "stability": WeightedScore(
                {
                    "low_volatility_score": snapshot.value("low_volatility_score"),
                    "low_drawdown_score": snapshot.value("low_drawdown_score"),
                },
                policy.local_component_weights["stability"],
            ),
            "market_state": {"risk_on": 60.0, "neutral": 50.0, "risk_off": 40.0}.get(snapshot.market_regime, 50.0),
            "entry_quality": snapshot.value("entry_quality"),
        }
    elif policy.strategy is Strategy.D25:
        components = {
            "trend": WeightedScore(
                {
                    "ma20_60_structure": snapshot.value("ma20_60_structure"),
                    "ma_slope": snapshot.value("ma_slope"),
                    "breakout_20d": snapshot.value("breakout_20d"),
                },
                policy.local_component_weights["trend"],
            ),
            "quality_value": WeightedScore(
                {
                    "quality_score": snapshot.value("quality_score"),
                    "value_score": snapshot.value("value_score"),
                    "growth_score": snapshot.value("growth_score"),
                },
                policy.local_component_weights["quality_value"],
            ),
            "stability": WeightedScore(
                {
                    "low_volatility_score": snapshot.value("low_volatility_score"),
                    "low_drawdown_score": snapshot.value("low_drawdown_score"),
                },
                policy.local_component_weights["stability"],
            ),
            "flow_liquidity": WeightedScore(
                {
                    "amount_percentile_20d": snapshot.value("amount_percentile_20d"),
                    "turnover_shock_score": snapshot.value("turnover_shock_score"),
                    "amount_shock_score": snapshot.value("amount_shock_score"),
                },
                policy.local_component_weights["flow_liquidity"],
            ),
            "entry_quality": snapshot.value("entry_quality"),
        }
    else:
        raise ValueError("long has no board score")
    result = Compose(components, policy.local_weights)
    rules = tuple(
        MissingFieldRule(field, MissingSeverity.GENERAL, Decimal(str(weight * inner_weight * 50.0)), False)
        for component, weight in policy.local_weights.items()
        if component != "market_state"
        for field, inner_weight in (
            ((component, 1.0),) if component == "entry_quality" else policy.local_component_weights[component].items()
        )
    )
    quality = AssessMissingValues(snapshot, MissingValuePolicy(rules))
    penalty = float(quality.quality_penalty)
    financial_weights = policy.local_component_weights.get("quality_value", {})
    financial_contribution = sum(
        (
            (snapshot.optional_value(key) or 0.0) * weight * policy.local_weights.get("quality_value", 0.0)
            for key, weight in financial_weights.items()
        ),
        0.0,
    )
    growth_contribution = (
        (snapshot.optional_value("growth_score") or 0.0)
        * financial_weights.get("growth_score", 0.0)
        * policy.local_weights.get("quality_value", 0.0)
    )
    return LocalScoreResult(
        components={
            **result.components,
            "quality_penalty": penalty,
            "financial_contribution": financial_contribution,
            "growth_contribution": growth_contribution,
        },
        base_score=Clamp(result.base_score - penalty),
    )


def _weighted_known(
    snapshot: FeatureSnapshot,
    fields: tuple[str, ...],
    weights: Mapping[str, float],
) -> float:
    if set(weights) != set(fields):
        raise ValueError("known-value component weights do not match its fields")
    known = tuple(field for field in fields if snapshot.optional_value(field) is not None)
    if not known:
        return 50.0
    known_weight = sum(weights[field] for field in known)
    if known_weight <= 0.0:
        return 50.0
    return Clamp(sum(snapshot.value(field) * weights[field] for field in known) / known_weight)


__all__ = [
    "BOARD_SCHEMA_VERSION",
    "BoardCrossSection",
    "BoardCrossSectionRequest",
    "MAX_FALLBACK_SESSIONS",
    "MIN_BOARD_SAMPLE",
    "ApplyBoardPolicy",
    "BuildBoardCrossSection",
    "EnrichBoardFeatures",
    "ProjectBoardPolicy",
    "ScoreBoardStrategy",
    "SupportedWeight",
]
