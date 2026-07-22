"""Pure board-relative recommendation scoring."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Literal

from trader.domain.market.factors import band_score, clamp
from trader.domain.market.models import (
    Board,
    BoardPopulation,
    CrossSectionStats,
    FeatureSnapshot,
)
from trader.domain.recommendation.models import (
    BoardStrategyPolicy,
    Strategy,
)
from trader.domain.recommendation.scoring_support import (
    MAX_FALLBACK_SESSIONS,
    MIN_BOARD_SAMPLE,
    _base_reliability,
    _candidate_fields,
    _default_competition_group,
    _distributions,
    _leader_gaps,
    _liquidity_bucket,
    _mean_known,
    _normalize_features,
    _peer_gaps,
    _population_version,
    _PopulationVersionIdentity,
    _quantile,
    _supported_weight,
    _value_or_missing,
    candidate_fields,
    supported_weight,
)
from trader.domain.recommendation.strategies.composition import LocalScoreResult, compose

BOARD_SCHEMA_VERSION = "board_cross_section_v16"


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


def build_board_cross_section(request: BoardCrossSectionRequest) -> BoardCrossSection:
    """Build one isolated board cross-section without reading configuration or I/O."""

    _validate_request(request)
    ordered = tuple(
        sorted(
            (item for item in request.features if item.quote.board is request.board),
            key=lambda item: item.quote.code,
        )
    )
    peer_raw = _peer_gaps(ordered)
    leader_raw = _leader_gaps(ordered)
    current_distributions = _distributions(ordered, peer_raw, leader_raw)
    current_sample = sum(
        item.optional_value("amount_median_20d") is not None and item.value("amount_median_20d") > 0.0
        for item in ordered
    )
    basis = _population_basis(request, current_distributions, current_sample)
    normalization_version = (
        request.fallback.population.population_version
        if basis.status == "fallback" and request.fallback is not None
        else request.data_version
    )
    normalized, normalization = _normalize_features(
        ordered,
        peer_raw,
        leader_raw,
        basis.reference_values,
        normalization_version,
    )
    population = _build_population(request, basis, current_sample, len(ordered))
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
        and fallback.population.status == "current"
        and fallback.population.sample_size >= MIN_BOARD_SAMPLE
        and fallback_age is not None
        and 0 <= fallback_age <= MAX_FALLBACK_SESSIONS
        and fallback.population.trade_date != request.trade_date
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
    )
    population_version = _population_version(identity, basis.population_source)
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


def apply_board_policy(
    cross_section: BoardCrossSection,
    strategy: Strategy,
    policy: BoardStrategyPolicy,
) -> tuple[FeatureSnapshot, ...]:
    if policy.strategy is not strategy or policy.board is not cross_section.board:
        raise ValueError("board policy does not match cross-section")
    result: list[FeatureSnapshot] = []
    for item in cross_section.features:
        supported = _supported_weight(strategy, item.values, policy.local_weights)
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


def enrich_board_features(
    strategy: Strategy,
    policy: BoardStrategyPolicy,
    request: BoardCrossSectionRequest,
) -> tuple[FeatureSnapshot, ...]:
    """Build and apply one board policy for callers without a cross-section cache."""

    if request.board is not policy.board:
        raise ValueError("board cross-section request does not match policy")
    cross_section = build_board_cross_section(request)
    return apply_board_policy(cross_section, strategy, policy)


def board_candidate_score(snapshot: FeatureSnapshot, policy: BoardStrategyPolicy) -> float:
    if snapshot.quote.board is not policy.board or policy.strategy is Strategy.LONG:
        raise ValueError("board candidate policy does not match snapshot")
    completeness = 100.0 * (1.0 - snapshot.missing_ratio(tuple(_candidate_fields(policy.strategy))))
    if policy.strategy is Strategy.TODAY:
        values = {
            "liquidity": snapshot.value("amount_percentile_20d"),
            "intraday_structure": clamp(
                0.35 * band_score(snapshot.quote.change_5m, 0.0, 0.2, 1.8, 3.5)
                + 0.25 * snapshot.value("speed_percentile")
                + 0.20 * band_score(snapshot.quote.pct_change, -1.0, 1.0, 5.5, 8.0)
                + 0.20 * band_score(snapshot.quote.volume_ratio, 0.8, 1.2, 3.5, 6.0)
            ),
            "turnover_state": _mean_known(snapshot, ("turnover_shock_score", "amount_shock_score")),
            "peer_gap": _mean_known(snapshot, ("peer_gap_1d_score", "peer_gap_3d_score", "peer_gap_5d_score")),
            "data_completeness": completeness,
        }
    elif policy.strategy is Strategy.TOMORROW:
        values = {
            "liquidity": snapshot.value("amount_percentile_20d"),
            "peer_gap": _mean_known(snapshot, ("peer_gap_5d_score", "peer_gap_20d_score")),
            "trend": snapshot.value("trend_score"),
            "stability": _mean_known(snapshot, ("low_volatility_score", "low_drawdown_score")),
            "data_completeness": completeness,
        }
    else:
        values = {
            "liquidity": snapshot.value("amount_percentile_20d"),
            "residual_momentum": _mean_known(snapshot, ("peer_gap_20d_score", "peer_gap_60d_score")),
            "trend": snapshot.value("trend_score"),
            "stability": _mean_known(snapshot, ("low_volatility_score", "low_drawdown_score")),
            "execution": _mean_known(snapshot, ("capacity_score", "moderate_amplitude", "price_executability")),
            "data_completeness": completeness,
        }
    return compose(values, policy.candidate_weights).base_score


def score_board_strategy(snapshot: FeatureSnapshot, policy: BoardStrategyPolicy) -> LocalScoreResult:
    if snapshot.quote.board is not policy.board:
        raise ValueError("board score policy does not match snapshot board")
    if policy.strategy is Strategy.TODAY:
        components = {
            "intraday_structure": clamp(
                0.30 * band_score(snapshot.quote.change_5m, 0.0, 0.2, 1.8, 3.5)
                + 0.20 * snapshot.value("speed_percentile")
                + 0.20 * band_score(snapshot.quote.pct_change, -1.0, 1.0, 5.5, 8.0)
                + 0.15 * band_score(snapshot.quote.volume_ratio, 0.8, 1.2, 3.5, 6.0)
                + 0.15 * snapshot.value("relative_strength_3d")
            ),
            "turnover_state": _mean_known(
                snapshot, ("turnover_shock_score", "amount_shock_score", "flow_confirmation_score")
            ),
            "peer_gap": _mean_known(snapshot, ("peer_gap_1d_score", "peer_gap_3d_score", "peer_gap_5d_score")),
            "liquidity_execution": clamp(
                0.60 * snapshot.value("amount_percentile_20d")
                + 0.20 * band_score(snapshot.quote.turnover_rate, 0.5, 1.5, 8.0, 15.0)
                + 0.20 * snapshot.value("limit_distance_safety")
            ),
            "stability": _mean_known(snapshot, ("low_volatility_score", "low_drawdown_score")),
        }
    elif policy.strategy is Strategy.TOMORROW:
        components = {
            "tail_structure": clamp(
                0.35 * snapshot.value("tail_return_30m")
                + 0.30 * snapshot.value("tail_volume_ratio")
                + 0.35 * snapshot.value("close_location")
            ),
            "peer_leader": clamp(
                0.40 * snapshot.value("peer_gap_5d_score")
                + 0.40 * snapshot.value("peer_gap_20d_score")
                + 0.20 * snapshot.value("leader_gap_score")
            ),
            "turnover_flow": clamp(
                0.35 * snapshot.value("turnover_shock_score")
                + 0.35 * snapshot.value("amount_shock_score")
                + 0.30 * snapshot.value("flow_confirmation_score")
            ),
            "trend": clamp(
                0.30 * snapshot.value("ma20_60_position")
                + 0.30 * snapshot.value("ma_slope")
                + 0.20 * snapshot.value("breakout_20d")
                + 0.20 * snapshot.value("industry_trend")
            ),
            "stability": _mean_known(snapshot, ("low_volatility_score", "low_drawdown_score")),
            "market_state": {"risk_on": 60.0, "neutral": 50.0, "risk_off": 40.0}.get(snapshot.market_regime, 50.0),
            "entry_quality": snapshot.value("entry_quality"),
        }
    elif policy.strategy is Strategy.D25:
        components = {
            "residual_momentum": clamp(
                0.60 * snapshot.value("peer_gap_20d_score") + 0.40 * snapshot.value("peer_gap_60d_score")
            ),
            "trend": clamp(
                0.35 * snapshot.value("ma20_60_structure")
                + 0.30 * snapshot.value("ma_slope")
                + 0.20 * snapshot.value("breakout_20d")
                + 0.15 * snapshot.value("industry_trend")
            ),
            "quality_value": clamp(
                0.50 * snapshot.value("quality_score")
                + 0.30 * snapshot.value("value_score")
                + 0.20 * snapshot.value("growth_score")
            ),
            "stability": _mean_known(snapshot, ("low_volatility_score", "low_drawdown_score")),
            "flow_liquidity": clamp(
                0.50 * snapshot.value("amount_percentile_20d")
                + 0.25 * snapshot.value("turnover_shock_score")
                + 0.25 * snapshot.value("amount_shock_score")
            ),
            "entry_quality": snapshot.value("entry_quality"),
        }
    else:
        raise ValueError("long has no board score")
    return compose(components, policy.local_weights)


__all__ = [
    "BOARD_SCHEMA_VERSION",
    "BoardCrossSection",
    "BoardCrossSectionRequest",
    "MAX_FALLBACK_SESSIONS",
    "MIN_BOARD_SAMPLE",
    "apply_board_policy",
    "board_candidate_score",
    "build_board_cross_section",
    "candidate_fields",
    "enrich_board_features",
    "score_board_strategy",
    "supported_weight",
]
