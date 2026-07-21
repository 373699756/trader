"""Pure board-relative candidate and local-score calculations for v16."""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Literal

from trader.domain.factors import band_score, clamp, percentile_scores_with_metadata
from trader.domain.models import (
    Board,
    BoardPopulation,
    BoardStrategyPolicy,
    CrossSectionStats,
    FeatureSnapshot,
    Strategy,
)
from trader.domain.strategies.composition import LocalScoreResult, compose, normalized

BOARD_SCHEMA_VERSION = "board_cross_section_v16"
MIN_BOARD_SAMPLE = 100
MAX_FALLBACK_SESSIONS = 5

_CANDIDATE_INPUTS: Mapping[Strategy, Mapping[str, tuple[str, ...]]] = {
    Strategy.TODAY: {
        "liquidity": ("amount_percentile_20d",),
        "intraday_structure": ("speed_percentile", "relative_strength_3d"),
        "turnover_state": ("turnover_shock_score", "amount_shock_score"),
        "peer_gap": ("peer_gap_1d_score", "peer_gap_3d_score", "peer_gap_5d_score"),
        "data_completeness": (),
    },
    Strategy.TOMORROW: {
        "liquidity": ("amount_percentile_20d",),
        "peer_gap": ("peer_gap_5d_score", "peer_gap_20d_score"),
        "trend": ("trend_score",),
        "stability": ("low_volatility_score", "low_drawdown_score"),
        "data_completeness": (),
    },
    Strategy.D25: {
        "liquidity": ("amount_percentile_20d",),
        "residual_momentum": ("peer_gap_20d_score", "peer_gap_60d_score"),
        "trend": ("trend_score",),
        "stability": ("low_volatility_score", "low_drawdown_score"),
        "execution": ("capacity_score", "moderate_amplitude", "price_executability"),
        "data_completeness": (),
    },
}

_LOCAL_COMPONENT_INPUTS: Mapping[Strategy, Mapping[str, tuple[str, ...]]] = {
    Strategy.TODAY: {
        "intraday_structure": ("speed_percentile", "relative_strength_3d"),
        "turnover_state": ("turnover_shock_score", "amount_shock_score", "flow_confirmation_score"),
        "peer_gap": ("peer_gap_1d_score", "peer_gap_3d_score", "peer_gap_5d_score"),
        "liquidity_execution": ("amount_percentile_20d", "limit_distance_safety"),
        "stability": ("low_volatility_score", "low_drawdown_score"),
    },
    Strategy.TOMORROW: {
        "tail_structure": ("tail_return_30m", "tail_volume_ratio", "close_location"),
        "peer_leader": ("peer_gap_5d_score", "peer_gap_20d_score", "leader_gap_score"),
        "turnover_flow": ("turnover_shock_score", "amount_shock_score", "flow_confirmation_score"),
        "trend": ("ma20_60_position", "ma_slope", "breakout_20d", "industry_trend"),
        "stability": ("low_volatility_score", "low_drawdown_score"),
        "market_state": (),
    },
    Strategy.D25: {
        "residual_momentum": ("peer_gap_20d_score", "peer_gap_60d_score"),
        "trend": ("ma20_60_structure", "ma_slope", "breakout_20d", "industry_trend"),
        "quality_value": ("quality_score", "value_score", "growth_score"),
        "stability": ("low_volatility_score", "low_drawdown_score"),
        "flow_liquidity": ("amount_percentile_20d", "turnover_shock_score", "amount_shock_score"),
        "not_overheated": ("return_20d_not_overheated",),
    },
}

_NORMALIZED_FIELDS: Mapping[str, tuple[str, bool]] = {
    "amount_median_20d": ("amount_percentile_20d", False),
    "speed": ("speed_percentile", False),
    "return_3d": ("relative_strength_3d", False),
    "return_5d": ("relative_strength_5d", False),
    "return_10d": ("relative_strength_10d", False),
    "return_20d": ("relative_strength_20d", False),
    "volatility_20d": ("low_volatility_score", True),
    "max_drawdown_20d": ("low_drawdown_score", False),
    "leader_gap": ("leader_gap_score", True),
    "peer_gap_1d": ("peer_gap_1d_score", False),
    "peer_gap_3d": ("peer_gap_3d_score", False),
    "peer_gap_5d": ("peer_gap_5d_score", False),
    "peer_gap_20d": ("peer_gap_20d_score", False),
    "peer_gap_60d": ("peer_gap_60d_score", False),
}


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
        amount
        for item in ordered
        if (amount := item.optional_value("amount_median_20d")) is not None and amount > 0.0
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
        fallback.population.population_version
        if status == "fallback" and fallback is not None
        else data_version
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
            "market_state": {"risk_on": 60.0, "neutral": 50.0, "risk_off": 40.0}.get(
                snapshot.market_regime, 50.0
            ),
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
            "not_overheated": snapshot.value("return_20d_not_overheated"),
        }
    else:
        raise ValueError("long has no board score")
    return compose(components, policy.local_weights)


def _normalize_features(
    features: Sequence[FeatureSnapshot],
    peer_raw: Mapping[str, Mapping[str, float | None]],
    leader_raw: Mapping[str, float | None],
    reference_values: Mapping[str, Sequence[float]],
    data_version: str,
) -> tuple[tuple[FeatureSnapshot, ...], Mapping[str, CrossSectionStats]]:
    raw_by_name: dict[str, dict[str, float | None]] = {
        name: {item.quote.code: _raw_field(item, name, peer_raw, leader_raw) for item in features}
        for name in _NORMALIZED_FIELDS
    }
    scores: dict[str, dict[str, float]] = {}
    stats: dict[str, CrossSectionStats] = {}
    for raw_name, (score_name, inverse) in _NORMALIZED_FIELDS.items():
        values = raw_by_name[raw_name]
        reference = tuple(reference_values.get(raw_name, ()))
        if reference and len(reference) >= MIN_BOARD_SAMPLE and _reference_is_needed(values, reference):
            score_map, metadata = _scores_against_reference(values, reference, inverse=inverse, data_version=data_version)
        else:
            score_map, metadata = percentile_scores_with_metadata(values, inverse=inverse, population_data_version=data_version)
        scores[score_name] = score_map
        stats[score_name] = metadata

    normalized: list[FeatureSnapshot] = []
    for item in features:
        values = dict(item.values)
        values.update(peer_raw[item.quote.code])
        values["leader_gap"] = leader_raw[item.quote.code]
        for raw_name, (score_name, _inverse) in _NORMALIZED_FIELDS.items():
            raw_value = raw_by_name[raw_name][item.quote.code]
            values[score_name] = scores[score_name][item.quote.code] if raw_value is not None else None
        turnover_shock = _turnover_shock(item)
        amount_shock = _amount_shock(item)
        values.update(
            {
                "turnover_shock_20": turnover_shock,
                "amount_shock_20": amount_shock,
                "turnover_shock_score": _band_or_none(turnover_shock, 0.8, 1.1, 2.0, 4.0),
                "amount_shock_score": _band_or_none(amount_shock, 0.8, 1.2, 3.0, 6.0),
                "flow_confirmation_score": _flow_confirmation(item, amount_shock),
            }
        )
        normalized.append(replace(item, values=values, normalization=stats))
    return tuple(normalized), stats


def _distributions(
    features: Sequence[FeatureSnapshot],
    peer_raw: Mapping[str, Mapping[str, float | None]],
    leader_raw: Mapping[str, float | None],
) -> dict[str, tuple[float, ...]]:
    result: dict[str, tuple[float, ...]] = {}
    for raw_name in _NORMALIZED_FIELDS:
        values = [
            _raw_field(item, raw_name, peer_raw, leader_raw)
            for item in features
        ]
        result[raw_name] = tuple(
            sorted(
                float(value)
                for value in values
                if value is not None
                and math.isfinite(value)
                and (raw_name != "amount_median_20d" or value > 0.0)
            )
        )
    result["leader_gap"] = tuple(sorted(float(value) for value in leader_raw.values() if value is not None and math.isfinite(value)))
    for name in ("peer_gap_1d", "peer_gap_3d", "peer_gap_5d", "peer_gap_20d", "peer_gap_60d"):
        peer_values = (item.get(name) for item in peer_raw.values())
        result[name] = tuple(sorted(value for value in peer_values if value is not None and math.isfinite(value)))
    return result


def _raw_field(
    item: FeatureSnapshot,
    name: str,
    peer_raw: Mapping[str, Mapping[str, float | None]],
    leader_raw: Mapping[str, float | None],
) -> float | None:
    if name.startswith("peer_gap_"):
        return peer_raw[item.quote.code].get(name)
    if name == "leader_gap":
        return leader_raw[item.quote.code]
    if name == "speed":
        return item.quote.speed
    return item.optional_value(name)


def _scores_against_reference(
    values: Mapping[str, float | None],
    reference: Sequence[float],
    *,
    inverse: bool,
    data_version: str,
) -> tuple[dict[str, float], CrossSectionStats]:
    finite = sorted(float(value) for value in reference if math.isfinite(float(value)))
    lower = _quantile(finite, 0.025)
    upper = _quantile(finite, 0.975)
    if lower is None or upper is None:
        raise ValueError("reference population must contain finite values")
    result: dict[str, float] = {}
    for key, value in values.items():
        if value is None or not math.isfinite(float(value)) or len(finite) < 2:
            result[key] = 50.0
            continue
        bounded = min(upper, max(lower, float(value)))
        less = sum(item < bounded for item in finite)
        equal = sum(item == bounded for item in finite)
        average_rank = less + (equal - 1) / 2.0
        score = average_rank * 100.0 / max(1, len(finite) - 1)
        result[key] = clamp(100.0 - score if inverse else score)
    missing_count = sum(not _finite_value(value) for value in values.values())
    return result, CrossSectionStats(lower, upper, len(finite), missing_count, 0.025, 0.975, data_version)


def _reference_is_needed(values: Mapping[str, float | None], reference: Sequence[float]) -> bool:
    return len(reference) >= MIN_BOARD_SAMPLE and sum(_finite_value(value) for value in values.values()) < MIN_BOARD_SAMPLE


def _peer_gaps(features: Sequence[FeatureSnapshot]) -> dict[str, dict[str, float | None]]:
    grouped: dict[str, list[FeatureSnapshot]] = defaultdict(list)
    for item in features:
        grouped[item.quote.industry.strip() or "unknown"].append(item)
    result: dict[str, dict[str, float | None]] = {item.quote.code: {} for item in features}
    horizons = {"1d": None, "3d": "return_3d", "5d": "return_5d", "20d": "return_20d", "60d": "return_60d"}
    for group in grouped.values():
        for suffix, field in horizons.items():
            for item in group:
                own = item.quote.pct_change if field is None else item.optional_value(field)
                peers = [
                    peer.quote.pct_change if field is None else peer.optional_value(field)
                    for peer in group
                    if peer.quote.code != item.quote.code
                ]
                finite = sorted(float(value) for value in peers if value is not None and math.isfinite(float(value)))
                result[item.quote.code][f"peer_gap_{suffix}"] = (
                    float(own) - _median(finite)
                    if len(finite) >= 10 and own is not None and math.isfinite(float(own))
                    else None
                )
    return result


def _leader_gaps(features: Sequence[FeatureSnapshot]) -> dict[str, float | None]:
    grouped: dict[str, list[FeatureSnapshot]] = defaultdict(list)
    for item in features:
        grouped[item.quote.industry.strip() or "unknown"].append(item)
    result: dict[str, float | None] = {}
    for group in grouped.values():
        ranked = sorted(
            (
                item
                for item in group
                if _positive_finite(item.optional_value("amount_median_20d"))
                and _finite_value(item.optional_value("return_20d"))
            ),
            key=lambda item: (-item.value("amount_median_20d"), item.quote.code),
        )
        leader_count = math.ceil(len(ranked) * 0.20)
        leaders = ranked[:leader_count]
        for item in group:
            own = item.optional_value("return_20d")
            peer_values = [
                leader.optional_value("return_20d")
                for leader in leaders
                if leader.quote.code != item.quote.code
            ]
            finite = [float(value) for value in peer_values if value is not None and math.isfinite(float(value))]
            result[item.quote.code] = (
                _mean(finite) - own
                if len(ranked) >= 10
                and len(leaders) >= 3
                and len(finite) >= 3
                and own is not None
                and _finite_value(own)
                else None
            )
    return result


def _turnover_shock(item: FeatureSnapshot) -> float | None:
    current = item.quote.turnover_rate
    baseline = item.optional_value("turnover_median_20d")
    if baseline is None:
        amount_median = item.optional_value("amount_median_20d")
        market_cap = item.quote.market_cap
        if amount_median is not None and market_cap is not None and math.isfinite(market_cap) and market_cap > 0.0:
            baseline = amount_median / market_cap * 100.0
    if (
        current is None
        or baseline is None
        or not math.isfinite(float(current))
        or not math.isfinite(float(baseline))
        or float(current) < 0.0
    ):
        return None
    if baseline <= 0.0:
        return None
    return float(current) / float(baseline)


def _amount_shock(item: FeatureSnapshot) -> float | None:
    amount = item.quote.amount
    median = item.optional_value("amount_median_20d")
    if amount is None or not math.isfinite(float(amount)) or amount < 0.0 or median is None or median <= 0.0:
        return None
    return float(amount) / float(median)


def _flow_confirmation(item: FeatureSnapshot, amount_shock: float | None) -> float | None:
    return_5d = item.optional_value("return_5d")
    if return_5d is None or amount_shock is None:
        return None
    direction = 1.0 if return_5d > 0.0 else -1.0 if return_5d < 0.0 else 0.0
    return clamp(50.0 + direction * min(50.0, amount_shock * 25.0))


def _band_or_none(value: float | None, lower: float, optimal_low: float, optimal_high: float, upper: float) -> float | None:
    return None if value is None or not math.isfinite(float(value)) else band_score(float(value), lower, optimal_low, optimal_high, upper)


def _mean_known(snapshot: FeatureSnapshot, fields: tuple[str, ...]) -> float:
    known = [snapshot.value(field) for field in fields if snapshot.optional_value(field) is not None]
    return sum(known) / len(known) if known else 50.0


def _candidate_fields(strategy: Strategy) -> tuple[str, ...]:
    fields: set[str] = set()
    for names in _CANDIDATE_INPUTS[strategy].values():
        fields.update(names)
    return tuple(sorted(fields))


def candidate_fields(strategy: Strategy) -> tuple[str, ...]:
    """Return the immutable quality-gate fields for one active strategy."""

    return _candidate_fields(strategy)


def supported_weight(strategy: Strategy, values: Mapping[str, float | None], weights: Mapping[str, float]) -> float:
    return _supported_weight(strategy, values, weights)


def _supported_weight(strategy: Strategy, values: Mapping[str, float | None], weights: Mapping[str, float]) -> float:
    supported = 0.0
    for component, weight in weights.items():
        inputs = _LOCAL_COMPONENT_INPUTS[strategy].get(component, ())
        if not inputs or all(_finite_value(values.get(name)) for name in inputs):
            supported += weight
    return min(1.0, max(0.0, supported))


def _base_reliability(values: Mapping[str, float | None]) -> float:
    fields = tuple(
        name
        for component_fields in _LOCAL_COMPONENT_INPUTS.values()
        for names in component_fields.values()
        for name in names
    )
    unique = tuple(dict.fromkeys(fields))
    known = sum(_finite_value(values.get(name)) for name in unique)
    return 1.0 if not unique else known / len(unique)


def _population_version(
    board: Board,
    trade_date: str,
    phase: str,
    data_version: str,
    schema_version: str,
    status: str,
    fallback_date: str | None,
    fallback_age: int | None,
    distributions: Mapping[str, Sequence[float]],
) -> str:
    material = repr(
        (
            board.value,
            trade_date,
            phase,
            data_version,
            schema_version,
            status,
            fallback_date,
            fallback_age,
            tuple((name, tuple(values)) for name, values in sorted(distributions.items())),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _liquidity_bucket(value: float | None, p50: float | None, p80: float | None) -> str:
    if value is None or not math.isfinite(value) or value <= 0.0 or p50 is None or p80 is None:
        return "unknown"
    if value < p50:
        return "liquidity_observe"
    if value < p80:
        return "normal_capacity"
    return "high_capacity"


def _quantile(values: Sequence[float], probability: float) -> float | None:
    finite = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not finite:
        return None
    position = (len(finite) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return finite[lower]
    return finite[lower] + (finite[upper] - finite[lower]) * (position - lower)


def _finite_value(value: float | None) -> bool:
    return value is not None and math.isfinite(float(value))


def _value_or_missing(value: float | None) -> bool:
    return not _finite_value(value)


def _positive_finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0.0


def _default_competition_group(item: FeatureSnapshot) -> tuple[str, str, str]:
    group = item.quote.industry.strip() or "unknown"
    return group, "coarse_industry_fallback", "industry:v1"


def _median(values: Sequence[float]) -> float:
    return values[len(values) // 2] if len(values) % 2 else (values[len(values) // 2 - 1] + values[len(values) // 2]) / 2.0


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


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
