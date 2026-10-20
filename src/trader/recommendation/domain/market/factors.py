"""Deterministic market-factor math and score normalization."""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from trader.recommendation.domain.market.feature_contracts import TOMORROW_RAW_ALPHA_FEATURE_MANIFEST
from trader.recommendation.domain.market.models import CrossSectionStats

PRODUCTION_FACTOR_IDS = frozenset(
    {
        "atr20_pct",
        "amount_median_20d",
        "amount_percentile_20d",
        "breakout_20d",
        "breakout_deviation_pct",
        "capacity_score",
        "close_location",
        "entry_quality",
        "financial_deterioration",
        "financial_fraud_history",
        "latest_disclosed_report_loss",
        "forced_delisting_risk",
        "fund_occupation_history",
        "growth_score",
        "industry_breadth",
        "industry_policy_score",
        "industry_strength",
        "industry_trend",
        "intraday_reversal",
        "illegal_guarantee_history",
        "limit_distance_safety",
        "limit_proximity",
        "liquidity_contraction",
        "low_drawdown_score",
        "low_volatility_score",
        "ma20_60_position",
        "ma20_60_structure",
        "ma20",
        "ma20_deviation_inverse",
        "ma20_slope_pct",
        "ma10",
        "ma5",
        "ma_slope",
        "market_breadth",
        "major_illegal_history",
        "major_shareholder_reduction",
        "max_drawdown_20d",
        "moderate_amplitude",
        "moderate_daily_return",
        "negative_announcement_level",
        "pledge_risk",
        "qfq_amihud_20d",
        "qfq_average_amount_20d",
        *TOMORROW_RAW_ALPHA_FEATURE_MANIFEST.names,
        "official_investigation_history",
        "price_executability",
        "price_volume_confirmation",
        "price_volume_divergence",
        "prior_high_20d",
        "quality_score",
        "reduction_or_unlock",
        "shareholder_reduction_level",
        "short_term_overheat",
        "relative_strength_10d",
        "relative_strength_20d",
        "relative_strength_5d",
        "return_10d",
        "return_20d",
        "return_5d",
        "return_60d",
        "risk_adjusted_return_20d",
        "risk_protection_score",
        "tail_return_30m",
        "tail_return_30m_pct",
        "tail_volume_ratio",
        "tail_volume_ratio_raw",
        "trend_score",
        "trend_breakdown",
        "corporate_risk_window_unavailable",
        "turnover_median_20d",
        "unlock_risk",
        "upward_consistency",
        "value_score",
        "volatility_20d",
        "volume_to_5d_average",
    }
)


def Clamp(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    if not math.isfinite(value):
        raise ValueError("score must be finite")
    if lower > upper:
        raise ValueError("lower bound cannot exceed upper bound")
    return min(upper, max(lower, value))


def RoundScore(value: float | Decimal, decimals: int = 2) -> float:
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError("score must be finite")
    bounded = min(Decimal("100"), max(Decimal("0"), parsed))
    quantum = Decimal(1).scaleb(-decimals)
    return float(bounded.quantize(quantum, rounding=ROUND_HALF_UP))


def BandScore(value: float | None, lower: float, optimal_low: float, optimal_high: float, upper: float) -> float:
    if not lower < optimal_low <= optimal_high < upper:
        raise ValueError("band boundaries must satisfy lower < optimal_low <= optimal_high < upper")
    if value is None or not math.isfinite(value):
        return 50.0
    if value <= lower or value >= upper:
        return 0.0
    if optimal_low <= value <= optimal_high:
        return 100.0
    if value < optimal_low:
        return 100.0 * (value - lower) / (optimal_low - lower)
    return 100.0 * (upper - value) / (upper - optimal_high)


def WeightedScore(values: Mapping[str, float], weights: Mapping[str, float]) -> float:
    if set(values) != set(weights):
        missing = sorted(set(weights) - set(values))
        extra = sorted(set(values) - set(weights))
        raise ValueError(f"component mismatch: missing={missing}, extra={extra}")
    if abs(sum(weights.values()) - 1.0) > 1e-9:
        raise ValueError("weights must sum to 1.0")
    return Clamp(sum(Clamp(values[name]) * weights[name] for name in weights))


def AverageRankPercentiles(values: Sequence[float]) -> tuple[float, ...]:
    """Return stable 0-1 average-rank percentiles aligned with the input."""

    parsed = tuple(float(value) for value in values)
    if any(not math.isfinite(value) for value in parsed):
        raise ValueError("percentile values must be finite")
    if not parsed:
        return ()
    if len(parsed) == 1:
        return (0.5,)

    order = sorted(range(len(parsed)), key=parsed.__getitem__)
    ranks = [0.0] * len(parsed)
    index = 0
    while index < len(order):
        end = index + 1
        while end < len(order) and parsed[order[end]] == parsed[order[index]]:
            end += 1
        average_rank = (index + end - 1) / 2.0
        percentile = average_rank / (len(parsed) - 1)
        for position in order[index:end]:
            ranks[position] = percentile
        index = end
    return tuple(ranks)


def PercentileScores(values: Mapping[str, float | None], *, inverse: bool = False) -> dict[str, float]:
    scores, _ = PercentileScoresWithMetadata(values, inverse=inverse)
    return scores


@dataclass(frozen=True)
class PercentileReference:
    """A fixed, sorted distribution shared by whole-population and subset projections."""

    values: tuple[float, ...]
    lower_quantile: float = 0.025
    upper_quantile: float = 0.975
    lower_bound: float | None = field(init=False)
    upper_bound: float | None = field(init=False)

    def __post_init__(self) -> None:
        if not 0.0 <= self.lower_quantile <= self.upper_quantile <= 1.0:
            raise ValueError("invalid winsor quantiles")
        finite = sorted(float(value) for value in self.values)
        if any(not math.isfinite(value) for value in finite):
            raise ValueError("percentile reference must contain finite values")
        lower = _quantile(finite, self.lower_quantile) if finite else None
        upper = _quantile(finite, self.upper_quantile) if finite else None
        bounded = (
            tuple(min(upper, max(lower, value)) for value in finite) if lower is not None and upper is not None else ()
        )
        object.__setattr__(self, "values", bounded)
        object.__setattr__(self, "lower_bound", lower)
        object.__setattr__(self, "upper_bound", upper)

    def rank(self, value: float) -> float:
        """Project onto this reference using average ties and the existing 0–1 scale."""
        if not math.isfinite(value):
            raise ValueError("percentile input must be finite")
        if not self.values:
            raise ValueError("percentile reference is empty")
        if len(self.values) == 1:
            return 0.5
        bounded = min(self.values[-1], max(self.values[0], value))
        left = bisect_left(self.values, bounded)
        right = bisect_right(self.values, bounded)
        return min(1.0, max(0.0, (left + right - 1) / (2.0 * (len(self.values) - 1))))

    def scores(
        self,
        values: Mapping[str, float | None],
        *,
        inverse: bool = False,
        population_data_version: str = "",
    ) -> tuple[dict[str, float], CrossSectionStats]:
        result: dict[str, float] = {}
        missing_count = 0
        for key, value in values.items():
            if value is None or not math.isfinite(value):
                missing_count += 1
                result[key] = 50.0
            elif not self.values:
                result[key] = 50.0
            else:
                rank = self.rank(float(value))
                result[key] = 100.0 * (1.0 - rank if inverse else rank)
        return result, CrossSectionStats(
            self.lower_bound,
            self.upper_bound,
            len(self.values),
            missing_count,
            self.lower_quantile,
            self.upper_quantile,
            population_data_version,
        )


def PercentileScoresWithMetadata(
    values: Mapping[str, float | None],
    *,
    inverse: bool = False,
    lower_quantile: float = 0.025,
    upper_quantile: float = 0.975,
    population_data_version: str = "",
) -> tuple[dict[str, float], CrossSectionStats]:
    reference = PercentileReference(
        tuple(float(value) for value in values.values() if value is not None and math.isfinite(value)),
        lower_quantile,
        upper_quantile,
    )
    return reference.scores(values, inverse=inverse, population_data_version=population_data_version)


def Winsorize(values: Iterable[float], lower_quantile: float = 0.025, upper_quantile: float = 0.975) -> list[float]:
    finite = sorted(float(value) for value in values if math.isfinite(value))
    if not finite:
        return []
    if not 0.0 <= lower_quantile <= upper_quantile <= 1.0:
        raise ValueError("invalid winsor quantiles")
    lower = _quantile(finite, lower_quantile)
    upper = _quantile(finite, upper_quantile)
    return [min(upper, max(lower, value)) for value in finite]


def InverseScore(value: float) -> float:
    return 100.0 - Clamp(value)


def _quantile(sorted_values: list[float], quantile: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = quantile * (len(sorted_values) - 1)
    lower_index = int(math.floor(position))
    upper_index = int(math.ceil(position))
    if lower_index == upper_index:
        return sorted_values[lower_index]
    fraction = position - lower_index
    return sorted_values[lower_index] * (1.0 - fraction) + sorted_values[upper_index] * fraction


__all__ = [
    "AverageRankPercentiles",
    "BandScore",
    "Clamp",
    "InverseScore",
    "PercentileReference",
    "PercentileScoresWithMetadata",
    "PRODUCTION_FACTOR_IDS",
    "PercentileScores",
    "RoundScore",
    "WeightedScore",
    "Winsorize",
]
