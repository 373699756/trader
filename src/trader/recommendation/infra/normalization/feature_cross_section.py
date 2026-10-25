"""One owner for population factors and complete-reference reuse in candidate reads."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from trader.recommendation.domain.market.factors import PercentileScoresWithMetadata
from trader.recommendation.domain.market.models import CrossSectionStats, MarketQuote

_FIELDS = (
    "amount_percentile_20d",
    "speed_percentile",
    "relative_strength_3d",
    "relative_strength_5d",
    "relative_strength_10d",
    "relative_strength_20d",
    "industry_strength",
    "industry_breadth",
    "industry_trend",
    "market_breadth",
    "low_volatility_score",
    "low_drawdown_score",
)
_NORMALIZED_FIELDS = frozenset(_FIELDS) - {"industry_breadth", "industry_trend"}


@dataclass(frozen=True)
class CrossSectionReferences:
    values: Mapping[str, Mapping[str, float | None]]
    normalization: Mapping[str, Mapping[str, CrossSectionStats]]


@dataclass(frozen=True)
class CrossSectionFeatures:
    values: Mapping[str, float | None]
    normalization: Mapping[str, CrossSectionStats]


def BuildCrossSectionFeatures(
    quotes: Sequence[MarketQuote],
    raw: Mapping[str, Mapping[str, float | None]],
    data_version: str,
    references: CrossSectionReferences,
) -> Mapping[str, CrossSectionFeatures]:
    """A complete population reference already owns every value and statistic."""
    if all(
        set(_FIELDS) <= references.values.get(quote.code, {}).keys()
        and _NORMALIZED_FIELDS <= references.normalization.get(quote.code, {}).keys()
        for quote in quotes
    ):
        return {
            quote.code: CrossSectionFeatures(
                MappingProxyType({name: references.values[quote.code][name] for name in _FIELDS}),
                references.normalization[quote.code],
            )
            for quote in quotes
        }
    amount_percentiles, amount_stats = PercentileScoresWithMetadata(
        {code: values.get("amount_median_20d") for code, values in raw.items()},
        population_data_version=data_version,
    )
    speed_percentiles, speed_stats = PercentileScoresWithMetadata(
        {quote.code: quote.speed for quote in quotes}, population_data_version=data_version
    )
    strength_results = {
        days: PercentileScoresWithMetadata(
            {code: values.get(f"return_{days}d") for code, values in raw.items()},
            population_data_version=data_version,
        )
        for days in (3, 5, 10, 20)
    }
    volatility_scores, volatility_stats = PercentileScoresWithMetadata(
        {code: values.get("volatility_20d") for code, values in raw.items()},
        inverse=True,
        population_data_version=data_version,
    )
    drawdown_scores, drawdown_stats = PercentileScoresWithMetadata(
        {code: values.get("max_drawdown_20d") for code, values in raw.items()},
        population_data_version=data_version,
    )
    industry_strength, industry_strength_stats, industry_breadth = _industry_scores(quotes, data_version)
    valid_changes = [
        quote.pct_change for quote in quotes if quote.pct_change is not None and math.isfinite(quote.pct_change)
    ]
    market_breadth = 100.0 * sum(value > 0 for value in valid_changes) / len(valid_changes) if valid_changes else None
    shared_normalization = {
        "amount_percentile_20d": amount_stats,
        "speed_percentile": speed_stats,
        "relative_strength_3d": strength_results[3][1],
        "relative_strength_5d": strength_results[5][1],
        "relative_strength_10d": strength_results[10][1],
        "relative_strength_20d": strength_results[20][1],
        "industry_strength": industry_strength_stats,
        "market_breadth": CrossSectionStats(
            None, None, len(valid_changes), len(quotes) - len(valid_changes), 0.025, 0.975, data_version
        ),
        "low_volatility_score": volatility_stats,
        "low_drawdown_score": drawdown_stats,
    }
    result: dict[str, CrossSectionFeatures] = {}
    for quote in quotes:
        values = raw[quote.code]
        reference = references.values.get(quote.code, {})
        cross_section = {
            "amount_percentile_20d": _if_present(values.get("amount_median_20d"), amount_percentiles[quote.code]),
            "speed_percentile": _if_present(quote.speed, speed_percentiles[quote.code]),
            **{
                f"relative_strength_{days}d": _if_present(
                    values.get(f"return_{days}d"), strength_results[days][0][quote.code]
                )
                for days in (3, 5, 10, 20)
            },
            "industry_strength": industry_strength.get(quote.industry),
            "industry_breadth": industry_breadth.get(quote.industry),
            "industry_trend": industry_strength.get(quote.industry),
            "market_breadth": market_breadth,
            "low_volatility_score": _if_present(values.get("volatility_20d"), volatility_scores[quote.code]),
            "low_drawdown_score": _if_present(values.get("max_drawdown_20d"), drawdown_scores[quote.code]),
        }
        cross_section.update((name, reference[name]) for name in _FIELDS if name in reference)
        result[quote.code] = CrossSectionFeatures(
            MappingProxyType(cross_section),
            MappingProxyType({**shared_normalization, **references.normalization.get(quote.code, {})}),
        )
    return result


def _industry_scores(
    quotes: Sequence[MarketQuote], data_version: str
) -> tuple[dict[str, float], CrossSectionStats, dict[str, float]]:
    changes: dict[str, list[float]] = defaultdict(list)
    for quote in quotes:
        if quote.industry and quote.pct_change is not None and math.isfinite(quote.pct_change):
            changes[quote.industry].append(quote.pct_change)
    averages = {industry: sum(values) / len(values) for industry, values in changes.items() if values}
    strength_by_industry, stats = PercentileScoresWithMetadata(averages, population_data_version=data_version)
    breadth = {
        industry: 100.0 * sum(value > 0 for value in values) / len(values)
        for industry, values in changes.items()
        if values
    }
    return strength_by_industry, stats, breadth


def _if_present(raw: float | None, score: float) -> float | None:
    return score if raw is not None and math.isfinite(raw) else None


__all__ = ["BuildCrossSectionFeatures", "CrossSectionFeatures", "CrossSectionReferences"]
