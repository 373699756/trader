"""Shadow scoring experiments for v10-like candidate pathways."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from statistics import median
from typing import TypedDict

from trader.domain.factors import band_score, clamp, weighted_score
from trader.domain.models import FeatureSnapshot, Recommendation, Strategy
from trader.domain.ranking import CORE_FIELDS
from trader.domain.strategies.composition import liquidity_score, normalized

SHADOW_REPORT_VERSION = "v10_candidate_shadow_v1"
SHADOW_MIN_INDUSTRY_SIZE = 10
SHADOW_MIN_LEADER_SIZE = 3

SHADOW_WEIGHTS = {
    Strategy.TODAY: {
        "liquidity": 0.30,
        "intraday_structure": 0.25,
        "turnover_status": 0.20,
        "peer_gap": 0.15,
        "completeness": 0.10,
    },
    Strategy.TOMORROW: {
        "liquidity": 0.30,
        "peer_gap": 0.25,
        "trend": 0.20,
        "stability": 0.15,
        "completeness": 0.10,
    },
    Strategy.D25: {
        "liquidity": 0.25,
        "residual_momentum": 0.25,
        "trend": 0.20,
        "stability": 0.15,
        "execution": 0.10,
        "completeness": 0.05,
    },
}


class _ShadowCandidate(TypedDict):
    code: str
    shadow_score: float
    local_score: float
    final_score: float
    components: dict[str, float]


def build_shadow_report(
    recommendations: Sequence[Recommendation],
    strategy: Strategy,
    production_codes: Sequence[str],
    *,
    top_n: int = 10,
) -> dict[str, object]:
    """Build v10-like shadow ranking metadata for review only."""
    if strategy not in SHADOW_WEIGHTS:
        return {"enabled": False, "version": SHADOW_REPORT_VERSION, "reason": "unsupported_strategy"}
    if not recommendations:
        return {"enabled": False, "version": SHADOW_REPORT_VERSION, "reason": "no_candidates"}

    selected_count = min(max(top_n, 0), len(recommendations))
    if selected_count == 0:
        return {"enabled": False, "version": SHADOW_REPORT_VERSION, "reason": "empty_selection"}

    peer_reference = _build_peer_reference(recommendations, strategy)
    scored = [_score_candidate(item, strategy=strategy, peer_reference=peer_reference) for item in recommendations]
    ranked_shadow = sorted(
        scored,
        key=lambda item: (
            -item["shadow_score"],
            -item["local_score"],
            item["code"],
        ),
    )
    shadow_top = ranked_shadow[:selected_count]
    production_index = {code: idx + 1 for idx, code in enumerate(production_codes)}
    overlap_codes = tuple(item["code"] for item in shadow_top if item["code"] in production_index)
    return {
        "enabled": True,
        "version": SHADOW_REPORT_VERSION,
        "strategy": strategy.value,
        "strategy_parameters": {
            "weights": dict(SHADOW_WEIGHTS[strategy]),
            "horizons": list(_horizon_plan(strategy)),
        },
        "scope": {
            "candidate_top_n": selected_count,
            "production_top_n": len(production_codes),
        },
        "selection": {
            "production_overlap_count": len(overlap_codes),
            "production_overlap_ratio": len(overlap_codes) / selected_count if selected_count else 0.0,
            "shadow_only_count": selected_count - len(overlap_codes),
            "top_shadows": [
                {
                    "code": item["code"],
                    "rank": rank + 1,
                    "shadow_score": item["shadow_score"],
                    "production_rank": production_index.get(item["code"], 0),
                    "local_score": item["local_score"],
                    "final_score": item["final_score"],
                }
                for rank, item in enumerate(shadow_top)
            ],
            "rank_gap": [
                {
                    "code": item["code"],
                    "shadow_rank": rank + 1,
                    "production_rank": production_index.get(item["code"], 0),
                    "rank_delta": (production_index.get(item["code"], selected_count + 1)) - (rank + 1),
                }
                for rank, item in enumerate(shadow_top)
            ],
        },
        "components": [
            {
                "code": item["code"],
                "shadow_score": item["shadow_score"],
                "components": {name: _round2(item["components"][name]) for name in sorted(item["components"])},
            }
            for item in shadow_top
        ],
        "coverage": {"overlap_codes": list(overlap_codes)},
    }


def _score_candidate(
    recommendation: Recommendation,
    *,
    strategy: Strategy,
    peer_reference: dict[str, dict[str, dict[str, float]]],
) -> _ShadowCandidate:
    features = recommendation.features
    if strategy is Strategy.TODAY:
        components = {
            "liquidity": liquidity_score(features),
            "intraday_structure": _today_structure_score(features),
            "turnover_status": _turnover_status_score(features.quote.turnover_rate),
            "peer_gap": _peer_gap_score(features, peer_reference, Strategy.TODAY),
            "completeness": _completeness_score(features, CORE_FIELDS),
        }
    elif strategy is Strategy.TOMORROW:
        components = {
            "liquidity": liquidity_score(features),
            "peer_gap": _peer_gap_score(features, peer_reference, Strategy.TOMORROW),
            "trend": _tomorrow_trend_score(features),
            "stability": _tomorrow_stability_score(features),
            "completeness": _completeness_score(features, CORE_FIELDS),
        }
    else:
        components = {
            "liquidity": liquidity_score(features),
            "residual_momentum": _d25_residual_momentum_score(features),
            "trend": _d25_trend_score(features),
            "stability": _d25_stability_score(features),
            "execution": _d25_execution_score(features),
            "completeness": _completeness_score(features, CORE_FIELDS),
        }

    final_components = {name: clamp(value) for name, value in components.items()}
    shadow_score = weighted_score(final_components, SHADOW_WEIGHTS[strategy])
    return {
        "code": features.quote.code,
        "shadow_score": _round2(shadow_score),
        "local_score": recommendation.score.local_score,
        "final_score": recommendation.score.final_score,
        "components": final_components,
    }


def _peer_gap_score(
    features: FeatureSnapshot,
    peer_reference: dict[str, dict[str, dict[str, float]]],
    strategy: Strategy,
) -> float:
    if strategy is Strategy.TODAY:
        values = [
            _peer_relative_gap(features, peer_reference.get("pct_change", {}), 0.35),
            _peer_relative_gap(features, peer_reference.get("return_3d", {}), 0.35),
            _peer_relative_gap(features, peer_reference.get("return_5d", {}), 0.30),
        ]
        return _combine_scores(values)
    if strategy is Strategy.TOMORROW:
        score_5d = _peer_relative_gap(features, peer_reference.get("return_5d", {}), 0.50)
        score_20d = _peer_relative_gap(features, peer_reference.get("return_20d", {}), 0.30)
        lead_score = _leader_gap(features, peer_reference.get("return_20d", {}), 0.20)
        scores = (score_5d, score_20d, lead_score)
        return _combine_scores(scores)
    score_20d = _peer_relative_gap(features, peer_reference.get("return_20d", {}), 0.55)
    score_60d = _peer_relative_gap(features, peer_reference.get("return_60d", {}), 0.45)
    return _combine_scores((score_20d, score_60d))


def _peer_relative_gap(
    features: FeatureSnapshot,
    field_map: dict[str, dict[str, float]],
    weight: float,
) -> tuple[float, float] | None:
    industry = field_map.get(features.quote.industry, {})
    code = features.quote.code
    code_value = industry.get(code)
    if code_value is None or not _is_finite(code_value):
        return None
    peer_values = [value for peer_code, value in industry.items() if peer_code != code and _is_finite(value)]
    if len(peer_values) < SHADOW_MIN_INDUSTRY_SIZE:
        return None
    gap = code_value - median(peer_values)
    return weight, _normalize_gap(gap)


def _leader_gap(
    features: FeatureSnapshot,
    field_map: dict[str, dict[str, float]],
    weight: float,
) -> tuple[float, float] | None:
    industry = field_map.get(features.quote.industry, {})
    code = features.quote.code
    code_value = industry.get(code)
    if code_value is None or not _is_finite(code_value):
        return None
    peer_values = sorted(value for peer_code, value in industry.items() if peer_code != code and _is_finite(value))
    if not peer_values or len(peer_values) < SHADOW_MIN_LEADER_SIZE:
        return None
    leader_count = max(SHADOW_MIN_LEADER_SIZE, math.ceil(len(peer_values) * 0.20))
    leader_return = sum(peer_values[-leader_count:]) / leader_count
    gap = leader_return - code_value
    return weight, _normalize_gap(gap)


def _combine_scores(scores: Sequence[tuple[float, float] | None]) -> float:
    weighted = [
        weight * score
        for item in scores
        if item is not None and (weight := item[0]) is not None and (score := item[1]) is not None
    ]
    if not weighted:
        return 50.0
    return clamp(sum(weighted) / sum(item[0] for item in scores if item is not None))


def _build_peer_reference(
    recommendations: Sequence[Recommendation],
    strategy: Strategy,
) -> dict[str, dict[str, dict[str, float]]]:
    fields = _horizon_fields(strategy) | {"pct_change"}
    reference: dict[str, dict[str, dict[str, float]]] = {field: defaultdict(dict) for field in fields}
    for item in recommendations:
        code = item.features.quote.code
        industry = item.features.quote.industry
        if industry is None:
            continue
        for field in fields:
            raw = item.features.values.get(field)
            if raw is None:
                continue
            if not _is_finite(raw):
                continue
            reference[field][industry][code] = float(raw)
        pct_change = item.features.quote.pct_change
        if pct_change is not None and _is_finite(pct_change):
            reference["pct_change"][industry][code] = float(pct_change)
    return reference


def _horizon_fields(strategy: Strategy) -> frozenset[str]:
    if strategy is Strategy.TODAY:
        return frozenset({"return_3d", "return_5d"})
    if strategy is Strategy.TOMORROW:
        return frozenset({"return_5d", "return_20d"})
    return frozenset({"return_20d", "return_60d"})


def _horizon_plan(strategy: Strategy) -> tuple[str, ...]:
    if strategy is Strategy.TODAY:
        return ("1/3/5",)
    if strategy is Strategy.TOMORROW:
        return ("5/20",)
    return ("20/60",)


def _today_structure_score(features: FeatureSnapshot) -> float:
    return clamp(
        0.40 * band_score(features.quote.change_5m, -0.2, -0.02, 0.2, 1.2)
        + 0.30 * normalized(features, "speed_percentile")
        + 0.30 * normalized(features, "relative_strength_5d")
    )


def _tomorrow_trend_score(features: FeatureSnapshot) -> float:
    return clamp(
        0.35 * normalized(features, "ma20_60_position")
        + 0.30 * normalized(features, "ma_slope")
        + 0.20 * normalized(features, "breakout_20d")
        + 0.15 * normalized(features, "industry_trend")
    )


def _tomorrow_stability_score(features: FeatureSnapshot) -> float:
    return clamp(
        0.35 * normalized(features, "low_volatility_score")
        + 0.35 * normalized(features, "low_drawdown_score")
        + 0.30 * normalized(features, "upward_consistency")
    )


def _d25_trend_score(features: FeatureSnapshot) -> float:
    return clamp(
        0.35 * normalized(features, "ma20_60_structure")
        + 0.30 * normalized(features, "ma_slope")
        + 0.20 * normalized(features, "industry_trend")
        + 0.15 * normalized(features, "breakout_20d")
    )


def _d25_stability_score(features: FeatureSnapshot) -> float:
    return clamp(
        0.35 * normalized(features, "low_volatility_score")
        + 0.30 * normalized(features, "low_drawdown_score")
        + 0.35 * normalized(features, "upward_consistency")
    )


def _d25_residual_momentum_score(features: FeatureSnapshot) -> float:
    return clamp(
        0.40 * normalized(features, "risk_adjusted_return_20d")
        + 0.30 * normalized(features, "relative_strength_20d")
        + 0.30 * normalized(features, "return_20d_not_overheated")
    )


def _d25_execution_score(features: FeatureSnapshot) -> float:
    return clamp(
        0.45 * normalized(features, "capacity_score")
        + 0.25 * normalized(features, "moderate_amplitude")
        + 0.30 * normalized(features, "price_executability")
    )


def _turnover_status_score(value: float | None) -> float:
    if value is None or not _is_finite(value):
        return 50.0
    return band_score(value, 0.5, 1.2, 4.0, 16.0)


def _completeness_score(features: FeatureSnapshot, required_fields: Sequence[str]) -> float:
    return clamp(100.0 * (1.0 - features.missing_ratio(tuple(required_fields))))


def _normalize_gap(gap: float) -> float:
    return clamp(band_score(gap, -4.0, -0.6, 0.6, 4.0))


def _is_finite(value: object) -> bool:
    if not isinstance(value, (int, float)):
        return False
    return not (math.isnan(float(value)) or math.isinf(float(value)))


def _round2(value: float) -> float:
    return round(float(value), 6)
