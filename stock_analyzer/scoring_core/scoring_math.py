from __future__ import annotations

from typing import Dict, Iterable, List, Tuple

import pandas as pd

from . import base as _base


__all__ = [
    "_attach_expected_return_prediction",
    "_close_location",
    "_combine",
    "_combine_details",
    "_combined_speed",
    "_composite_score",
    "_execution_score",
    "_factor_ic_multiplier",
    "_factor_ic_payload",
    "_has_signal",
    "_horizon_meta",
    "_horizon_row",
    "_hot_rank_score",
    "_market_regime_adjustment",
    "_not_overextended_score",
    "_optional_factor_score",
    "_regime_component",
    "_regime_component_from_profile",
    "_regime_weight",
    "_regime_weight_profile",
    "_row_speed",
    "_safe_corr",
    "_score_context",
    "_score_row",
    "_stddev",
    "_sum_penalty",
    "_swing_risk_penalty",
    "_swing_risk_penalty_parts",
    "_tail_close_setup_score",
    "_weighted_score",
]


def _score_context(df: pd.DataFrame, industry_strength: Dict[str, float]) -> Dict[str, List[float]]:
    return _base._score_context(df, industry_strength)


def _stddev(values: List[float]) -> float:
    return _base._stddev(values)


def _safe_corr(left: List[float], right: List[float]) -> float:
    return _base._safe_corr(left, right)


def _combined_speed(df: pd.DataFrame) -> pd.Series:
    return _base._combined_speed(df)


def _row_speed(row: pd.Series) -> float:
    return _base._row_speed(row)


def _tail_close_setup_score(row: pd.Series) -> float:
    return _base._tail_close_setup_score(row)


def _close_location(price: float, high: float, low: float) -> float:
    return _base._close_location(price, high, low)


def _hot_rank_score(rank) -> float:
    return _base._hot_rank_score(rank)


def _optional_factor_score(
    value,
    values: List[float],
    higher_is_better: bool = True,
    fallback=None,
    fallback_values: List[float] = None,
) -> float:
    return _base._optional_factor_score(
        value,
        values,
        higher_is_better=higher_is_better,
        fallback=fallback,
        fallback_values=fallback_values,
    )


def _has_signal(values: List[float]) -> bool:
    return _base._has_signal(values)


def _composite_score(parts: List[float]) -> float:
    return _base._composite_score(parts)


def _weighted_score(pairs: Tuple[Tuple[object, float], ...], fallback: object = 50.0) -> float:
    return _base._weighted_score(pairs, fallback=fallback)


def _market_regime_adjustment(
    row: pd.Series,
    market_regime: Dict[str, object],
    strategy_style: str,
) -> float:
    return _base._market_regime_adjustment(row, market_regime, strategy_style)


def _regime_weight(key: str, market_regime: Dict[str, object], default: float = 1.0) -> float:
    return _base._regime_weight(key, market_regime, default=default)


def _regime_weight_profile(market_regime: Dict[str, object], keys: List[str]) -> Dict[str, float]:
    return _base._regime_weight_profile(market_regime, keys)


def _regime_component(score: float, key: str, market_regime: Dict[str, object]) -> float:
    return _base._regime_component(score, key, market_regime)


def _regime_component_from_profile(score: float, key: str, profile: Dict[str, object]) -> float:
    return _base._regime_component_from_profile(score, key, profile)


def _combine(
    components: Dict[str, object],
    strategy: str,
    weights: Dict[str, object] = None,
    market_regime: Dict[str, object] = None,
    row: pd.Series = None,
    regime_weight_profile: Dict[str, object] = None,
) -> float:
    return _base._combine(
        components,
        strategy,
        weights=weights,
        market_regime=market_regime,
        row=row,
        regime_weight_profile=regime_weight_profile,
    )


def _combine_details(
    components: Dict[str, object],
    strategy: str,
    weights: Dict[str, object] = None,
    market_regime: Dict[str, object] = None,
    row: pd.Series = None,
    regime_weight_profile: Dict[str, object] = None,
) -> Dict[str, float]:
    return _base._combine_details(
        components,
        strategy,
        weights=weights,
        market_regime=market_regime,
        row=row,
        regime_weight_profile=regime_weight_profile,
    )


def _factor_ic_multiplier(component: str) -> float:
    return _base._factor_ic_multiplier(component)


def _factor_ic_payload() -> Dict[str, object]:
    return _base._factor_ic_payload()


def _execution_score(row: pd.Series) -> float:
    return _base._execution_score(row)


def _not_overextended_score(row: pd.Series) -> float:
    return _base._not_overextended_score(row)


def _sum_penalty(parts: Dict[str, float]) -> float:
    return _base._sum_penalty(parts)


def _swing_risk_penalty(row: pd.Series) -> float:
    return _base._swing_risk_penalty(row)


def _swing_risk_penalty_parts(row: pd.Series) -> Dict[str, float]:
    return _base._swing_risk_penalty_parts(row)


def _horizon_meta(
    top_n: int,
    market_filter: str,
    candidate_count: int,
    strategy_version: str,
    strategy_label: str,
) -> Dict[str, object]:
    return _base._horizon_meta(top_n, market_filter, candidate_count, strategy_version, strategy_label)


def _horizon_row(row: pd.Series, scores: Dict[str, object]) -> Dict[str, object]:
    return _base._horizon_row(row, scores)


def _score_row(*args, **kwargs) -> Dict[str, object]:
    return _base._score_row(*args, **kwargs)


def _attach_expected_return_prediction(
    strategy_name: str,
    rows: List[Dict[str, object]],
    samples: Iterable[Dict[str, object]] = None,
    use_ranking: bool = False,
) -> List[Dict[str, object]]:
    return _base._attach_expected_return_prediction(
        strategy_name,
        rows,
        samples=samples,
        use_ranking=use_ranking,
    )
