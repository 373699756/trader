"""Pure helper calculations for board-relative scoring."""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace

from trader.domain.factors import band_score, clamp, percentile_scores_with_metadata
from trader.domain.models import Board, CrossSectionStats, FeatureSnapshot, Strategy

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
        "entry_quality": ("entry_quality",),
    },
    Strategy.D25: {
        "residual_momentum": ("peer_gap_20d_score", "peer_gap_60d_score"),
        "trend": ("ma20_60_structure", "ma_slope", "breakout_20d", "industry_trend"),
        "quality_value": ("quality_score", "value_score", "growth_score"),
        "stability": ("low_volatility_score", "low_drawdown_score"),
        "flow_liquidity": ("amount_percentile_20d", "turnover_shock_score", "amount_shock_score"),
        "entry_quality": ("entry_quality",),
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
            score_map, metadata = _scores_against_reference(
                values, reference, inverse=inverse, data_version=data_version
            )
        else:
            score_map, metadata = percentile_scores_with_metadata(
                values, inverse=inverse, population_data_version=data_version
            )
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
        values = [_raw_field(item, raw_name, peer_raw, leader_raw) for item in features]
        result[raw_name] = tuple(
            sorted(
                float(value)
                for value in values
                if value is not None and math.isfinite(value) and (raw_name != "amount_median_20d" or value > 0.0)
            )
        )
    result["leader_gap"] = tuple(
        sorted(float(value) for value in leader_raw.values() if value is not None and math.isfinite(value))
    )
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
    return (
        len(reference) >= MIN_BOARD_SAMPLE and sum(_finite_value(value) for value in values.values()) < MIN_BOARD_SAMPLE
    )


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
        for item in group:
            own = item.optional_value("return_20d")
            ranked_peers = [peer for peer in ranked if peer.quote.code != item.quote.code]
            leader_count = math.ceil(len(ranked_peers) * 0.20)
            leaders = ranked_peers[:leader_count]
            peer_values = [leader.optional_value("return_20d") for leader in leaders]
            finite = [float(value) for value in peer_values if value is not None and math.isfinite(float(value))]
            result[item.quote.code] = (
                _mean(finite) - own
                if len(ranked_peers) >= 10
                and leader_count >= 3
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


def _band_or_none(
    value: float | None, lower: float, optimal_low: float, optimal_high: float, upper: float
) -> float | None:
    return (
        None
        if value is None or not math.isfinite(float(value))
        else band_score(float(value), lower, optimal_low, optimal_high, upper)
    )


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
    return (
        values[len(values) // 2] if len(values) % 2 else (values[len(values) // 2 - 1] + values[len(values) // 2]) / 2.0
    )


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


__all__ = ["MAX_FALLBACK_SESSIONS", "MIN_BOARD_SAMPLE"]
