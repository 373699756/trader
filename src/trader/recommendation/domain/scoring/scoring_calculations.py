"""Pure calculations for recommendation scoring."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from trader.recommendation.domain.market.factors import BandScore, Clamp, PercentileReference
from trader.recommendation.domain.market.models import (
    Board,
    CrossSectionStats,
    FeatureSnapshot,
)
from trader.recommendation.domain.publication.models import Strategy

MIN_BOARD_SAMPLE = 100
MAX_FALLBACK_SESSIONS = 5

_CANDIDATE_INPUTS: Mapping[Strategy, Mapping[str, tuple[str, ...]]] = {
    Strategy.TOMORROW: {
        "liquidity": ("amount_percentile_20d",),
        "trend": ("trend_score",),
        "stability": ("low_volatility_score", "low_drawdown_score"),
        "data_completeness": (),
    },
    Strategy.D25: {
        "liquidity": ("amount_percentile_20d",),
        "trend": ("trend_score",),
        "stability": ("low_volatility_score", "low_drawdown_score"),
        "execution": ("capacity_score", "moderate_amplitude", "price_executability"),
        "data_completeness": (),
    },
}

_LOCAL_COMPONENT_INPUTS: Mapping[Strategy, Mapping[str, tuple[str, ...]]] = {
    Strategy.TOMORROW: {
        "tail_structure": ("tail_return_30m", "tail_volume_ratio", "close_location"),
        "turnover_flow": ("turnover_shock_score", "amount_shock_score", "flow_confirmation_score"),
        "trend": ("ma20_60_position", "ma_slope", "breakout_20d"),
        "stability": ("low_volatility_score", "low_drawdown_score"),
        "market_state": (),
        "entry_quality": ("entry_quality",),
    },
    Strategy.D25: {
        "trend": ("ma20_60_structure", "ma_slope", "breakout_20d"),
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
}


def _normalize_features(
    features: Sequence[FeatureSnapshot],
    reference_values: Mapping[str, Sequence[float]],
    data_version: str,
) -> tuple[tuple[FeatureSnapshot, ...], Mapping[str, CrossSectionStats]]:
    raw_by_name: dict[str, dict[str, float | None]] = {
        name: {item.quote.code: _raw_field(item, name) for item in features} for name in _NORMALIZED_FIELDS
    }
    scores: dict[str, dict[str, float]] = {}
    stats: dict[str, CrossSectionStats] = {}
    for raw_name, (score_name, inverse) in _NORMALIZED_FIELDS.items():
        values = raw_by_name[raw_name]
        reference = PercentileReference(tuple(reference_values[raw_name]))
        score_map, metadata = reference.scores(values, inverse=inverse, population_data_version=data_version)
        scores[score_name] = score_map
        stats[score_name] = metadata

    normalized: list[FeatureSnapshot] = []
    for item in features:
        values = dict(item.values)
        for raw_name, (score_name, _inverse) in _NORMALIZED_FIELDS.items():
            raw_value = raw_by_name[raw_name][item.quote.code]
            values[score_name] = (
                scores[score_name][item.quote.code]
                if raw_value is not None and stats[score_name].sample_size > 0
                else None
            )
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
) -> dict[str, tuple[float, ...]]:
    result: dict[str, tuple[float, ...]] = {}
    for raw_name in _NORMALIZED_FIELDS:
        values = [_raw_field(item, raw_name) for item in features]
        result[raw_name] = tuple(
            sorted(
                float(value)
                for value in values
                if value is not None and math.isfinite(value) and (raw_name != "amount_median_20d" or value > 0.0)
            )
        )
    return result


def _raw_field(
    item: FeatureSnapshot,
    name: str,
) -> float | None:
    if name == "speed":
        return item.quote.speed
    return item.optional_value(name)


def _turnover_shock(item: FeatureSnapshot) -> float | None:
    current = item.quote.turnover_rate
    baseline = item.optional_value("turnover_median_20d")
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
    return Clamp(50.0 + direction * min(50.0, amount_shock * 25.0))


def _band_or_none(
    value: float | None, lower: float, optimal_low: float, optimal_high: float, upper: float
) -> float | None:
    return (
        None
        if value is None or not math.isfinite(float(value))
        else BandScore(float(value), lower, optimal_low, optimal_high, upper)
    )


def _candidate_fields(strategy: Strategy) -> tuple[str, ...]:
    fields: set[str] = set()
    for names in _CANDIDATE_INPUTS[strategy].values():
        fields.update(names)
    return tuple(sorted(fields))


def CandidateFields(strategy: Strategy) -> tuple[str, ...]:
    """Return the immutable quality-gate fields for one active strategy."""

    return _candidate_fields(strategy)


def SupportedWeight(
    strategy: Strategy,
    values: Mapping[str, float | None],
    weights: Mapping[str, float],
    *,
    phase: str | None = None,
) -> float:
    return _supported_weight(strategy, values, weights, phase=phase)


def ReliabilityFields(
    strategy: Strategy,
    weights: Mapping[str, float],
    *,
    phase: str | None = None,
) -> tuple[str, ...]:
    fields: set[str] = set()
    for component in weights:
        if phase == "close_fallback" and strategy is Strategy.TOMORROW and component == "tail_structure":
            continue
        fields.update(_LOCAL_COMPONENT_INPUTS[strategy].get(component, ()))
    return tuple(sorted(fields))


def _supported_weight(
    strategy: Strategy,
    values: Mapping[str, float | None],
    weights: Mapping[str, float],
    *,
    phase: str | None = None,
) -> float:
    supported = 0.0
    applicable = 0.0
    for component, weight in weights.items():
        if phase == "close_fallback" and strategy is Strategy.TOMORROW and component == "tail_structure":
            continue
        applicable += weight
        inputs = _LOCAL_COMPONENT_INPUTS[strategy].get(component, ())
        if not inputs:
            supported += weight
            continue
        known = sum(_finite_value(values.get(name)) for name in inputs)
        supported += weight * known / len(inputs)
    reliability = supported / applicable if applicable > 0.0 else 1.0
    return min(1.0, max(0.0, reliability))


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


@dataclass(frozen=True)
class _PopulationVersionIdentity:
    board: Board
    trade_date: str
    phase: str
    data_version: str
    schema_version: str
    status: str
    fallback_date: str | None
    fallback_age: int | None
    merge_epoch: str
    reference_hash: str


def _population_version(
    identity: _PopulationVersionIdentity,
) -> str:
    coordinates = (
        identity.board.value,
        identity.trade_date,
        identity.phase,
        identity.data_version,
        identity.schema_version,
        identity.status,
        identity.fallback_date,
        identity.fallback_age,
        identity.merge_epoch,
        identity.reference_hash,
    )
    return f"population:{hashlib.sha256(repr(coordinates).encode()).hexdigest()}"


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
    return group, "coarse_industry_fallback", "industry:fallback"


def _median(values: Sequence[float]) -> float:
    return (
        values[len(values) // 2] if len(values) % 2 else (values[len(values) // 2 - 1] + values[len(values) // 2]) / 2.0
    )


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


__all__ = ["MAX_FALLBACK_SESSIONS", "MIN_BOARD_SAMPLE", "ReliabilityFields", "SupportedWeight"]
