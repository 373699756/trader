"""Pure board-relative candidate and local-score calculations for v16."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Literal

from trader.domain.board_scoring_support import (
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
    _quantile,
    _supported_weight,
    _value_or_missing,
    candidate_fields,
    supported_weight,
)
from trader.domain.factors import band_score, clamp
from trader.domain.models import (
    Board,
    BoardPopulation,
    BoardStrategyPolicy,
    CrossSectionStats,
    FeatureSnapshot,
    Strategy,
)
from trader.domain.strategies.composition import LocalScoreResult, compose

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


def build_board_cross_section(
    features: Sequence[FeatureSnapshot],
    *,
    board: Board,
    merge_epoch: str,
    trade_date: str,
    phase: str,
    data_version: str,
    schema_version: str = BOARD_SCHEMA_VERSION,
    fallback: BoardCrossSection | None = None,
    fallback_age_sessions: int | None = None,
    competition_groups: Mapping[str, tuple[str, str, str]] | None = None,
) -> BoardCrossSection:
    """Build one isolated board cross-section without reading configuration or I/O."""

    if board is Board.UNSUPPORTED:
        raise ValueError("unsupported board cannot be scored")
    if not all((merge_epoch, trade_date, phase, data_version, schema_version)):
        raise ValueError("board cross-section identity must not be empty")
    ordered = tuple(sorted((item for item in features if item.quote.board is board), key=lambda item: item.quote.code))
    if any(item.quote.board is not board for item in ordered):
        raise ValueError("board cross-section contains another board")

    peer_raw = _peer_gaps(ordered)
    leader_raw = _leader_gaps(ordered)
    current_distributions = _distributions(ordered, peer_raw, leader_raw)
    valid_liquidity = [
        amount for item in ordered if (amount := item.optional_value("amount_median_20d")) is not None and amount > 0.0
    ]
    current_sample = len(valid_liquidity)
    fallback_ok = (
        fallback is not None
        and fallback.population.status == "current"
        and fallback.population.sample_size >= MIN_BOARD_SAMPLE
        and fallback_age_sessions is not None
        and 0 <= fallback_age_sessions <= MAX_FALLBACK_SESSIONS
        and fallback.population.trade_date != trade_date
    )
    use_reference = current_sample < MIN_BOARD_SAMPLE and fallback_ok
    reference_values = fallback.reference_values if use_reference and fallback is not None else current_distributions

    status: Literal["current", "fallback", "stale", "insufficient"]
    population_source: Mapping[str, Sequence[float]]
    if current_sample >= MIN_BOARD_SAMPLE:
        status = "current"
        population_source = current_distributions
        fallback_date = None
        fallback_age = None
    elif use_reference and fallback is not None:
        status = "fallback"
        population_source = reference_values
        fallback_date = fallback.population.trade_date
        fallback_age = fallback_age_sessions
    elif fallback is not None and fallback_age_sessions is not None and fallback_age_sessions > MAX_FALLBACK_SESSIONS:
        status = "stale"
        population_source = current_distributions
        fallback_date = fallback.population.trade_date
        fallback_age = fallback_age_sessions
    else:
        status = "insufficient"
        population_source = current_distributions
        fallback_date = None
        fallback_age = None

    normalization_data_version = (
        fallback.population.population_version if status == "fallback" and fallback is not None else data_version
    )
    normalized, normalization = _normalize_features(
        ordered,
        peer_raw,
        leader_raw,
        reference_values,
        normalization_data_version,
    )

    population_version = _population_version(
        board,
        trade_date,
        phase,
        data_version,
        schema_version,
        status,
        fallback_date,
        fallback_age,
        population_source,
    )
    p50 = _quantile(population_source.get("amount_median_20d", ()), 0.50)
    p80 = _quantile(population_source.get("amount_median_20d", ()), 0.80)
    population = BoardPopulation(
        trade_date=trade_date,
        phase=phase,
        board=board,
        data_version=data_version,
        schema_version=schema_version,
        population_version=population_version,
        sample_size=current_sample,
        missing_count=max(0, len(ordered) - current_sample),
        liquidity_p50=p50,
        liquidity_p80=p80,
        fallback_trade_date=fallback_date,
        fallback_age_sessions=fallback_age,
        status=status,
    )
    enriched: list[FeatureSnapshot] = []
    for item in normalized:
        values = dict(item.values)
        reliability = _base_reliability(values)
        if status in {"stale", "insufficient"}:
            reliability = min(reliability, 0.84)
        bucket = _liquidity_bucket(item.optional_value("amount_median_20d"), p50, p80)
        group_id, group_source, group_version = (competition_groups or {}).get(
            item.quote.code,
            _default_competition_group(item),
        )
        missing_fields = tuple(sorted(name for name in values if _value_or_missing(values.get(name))))
        missing_reasons = {
            name: (
                "board cross-section sample is insufficient"
                if status in {"stale", "insufficient"} and name.endswith("_score")
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
                merge_epoch=merge_epoch,
                competition_group_id=group_id,
                competition_group_source=group_source,
                competition_group_version=group_version,
                liquidity_bucket=bucket,
                parameter_status=status,
            )
        )
    return BoardCrossSection(
        board=board,
        merge_epoch=merge_epoch,
        trade_date=trade_date,
        phase=phase,
        data_version=data_version,
        schema_version=schema_version,
        population=population,
        features=tuple(enriched),
        reference_values=reference_values,
        normalization=normalization,
    )


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
    features: Sequence[FeatureSnapshot],
    policy: BoardStrategyPolicy,
    *,
    merge_epoch: str,
    trade_date: str = "unknown-date",
    phase: str = "score",
    data_version: str | None = None,
    schema_version: str = BOARD_SCHEMA_VERSION,
    fallback: BoardCrossSection | None = None,
    fallback_age_sessions: int | None = None,
) -> tuple[FeatureSnapshot, ...]:
    """Compatibility wrapper used by callers that do not own the cache."""

    cross_section = build_board_cross_section(
        features,
        board=policy.board,
        merge_epoch=merge_epoch,
        trade_date=trade_date,
        phase=phase,
        data_version=data_version or merge_epoch,
        schema_version=schema_version,
        fallback=fallback,
        fallback_age_sessions=fallback_age_sessions,
    )
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
