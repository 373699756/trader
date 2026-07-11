from __future__ import annotations

from typing import Dict, List

import pandas as pd

from . import base as _base


__all__ = [
    "mark_backup_watch",
    "mark_tomorrow_backup_watch",
    "_agent_bear_cases",
    "_agent_bull_cases",
    "_append_unique_reason",
    "_attach_signal_explanation",
    "_build_agent_committee",
    "_build_long_term_reasons",
    "_build_position_reasons",
    "_build_reasons",
    "_build_serenity_profile",
    "_build_swing_reasons",
    "_build_tech_potential_reasons",
    "_build_tomorrow_reasons",
    "_chase_risk",
    "_data_coverage",
    "_decision_score",
    "_exit_action",
    "_failure_reasons",
    "_mark_tomorrow_intraday_watch",
    "_overextension_risk",
    "_sell_risk",
    "_trade_action",
    "_unique_strings",
    "_weighted_score",
    "_with_regime_reason",
]


def _append_unique_reason(row: Dict[str, object], reason: str) -> None:
    _base._append_unique_reason(row, reason)


def mark_backup_watch(row: Dict[str, object], label: str = "备选观察", reason: str = "") -> None:
    _base.mark_backup_watch(row, label=label, reason=reason)


def mark_tomorrow_backup_watch(
    row: Dict[str, object],
    label: str = "备选观察",
    reason: str = "",
) -> None:
    _base.mark_tomorrow_backup_watch(row, label=label, reason=reason)


def _mark_tomorrow_intraday_watch(row: Dict[str, object]) -> None:
    _base._mark_tomorrow_intraday_watch(row)


def _attach_signal_explanation(
    item: Dict[str, object],
    row: pd.Series,
    strategy_name: str,
    strategy_label: str,
    signal_label: str,
) -> Dict[str, object]:
    return _base._attach_signal_explanation(item, row, strategy_name, strategy_label, signal_label)


def _with_regime_reason(
    item: Dict[str, object],
    market_regime: Dict[str, object],
    regime_bonus: float,
) -> Dict[str, object]:
    return _base._with_regime_reason(item, market_regime, regime_bonus)


def _build_agent_committee(item: Dict[str, object], row: pd.Series) -> Dict[str, object]:
    return _base._build_agent_committee(item, row)


def _build_serenity_profile(item: Dict[str, object], row: pd.Series) -> Dict[str, object]:
    return _base._build_serenity_profile(item, row)


def _weighted_score(pairs, fallback: object = 50.0) -> float:
    return _base._weighted_score(pairs, fallback=fallback)


def _agent_bull_cases(
    item: Dict[str, object],
    technical_score: float,
    fundamentals_proxy_score: float,
    sentiment_score: float,
    liquidity_score: float,
) -> List[str]:
    return _base._agent_bull_cases(
        item,
        technical_score,
        fundamentals_proxy_score,
        sentiment_score,
        liquidity_score,
    )


def _agent_bear_cases(item: Dict[str, object], risk_score: float, news_environment_score: float) -> List[str]:
    return _base._agent_bear_cases(item, risk_score, news_environment_score)


def _decision_score(item: Dict[str, object], profile: Dict[str, object]) -> float:
    return _base._decision_score(item, profile)


def _sell_risk(item: Dict[str, object], row: pd.Series, profile: Dict[str, object]) -> Dict[str, object]:
    return _base._sell_risk(item, row, profile)


def _trade_action(item: Dict[str, object], profile: Dict[str, object]) -> Dict[str, object]:
    return _base._trade_action(item, profile)


def _exit_action(item: Dict[str, object], profile: Dict[str, object]) -> Dict[str, object]:
    return _base._exit_action(item, profile)


def _data_coverage(row: pd.Series) -> float:
    return _base._data_coverage(row)


def _unique_strings(values: List[object]) -> List[str]:
    return _base._unique_strings(values)


def _chase_risk(row: pd.Series) -> Dict[str, object]:
    return _base._chase_risk(row)


def _overextension_risk(row: pd.Series) -> Dict[str, object]:
    return _base._overextension_risk(row)


def _failure_reasons(*args, **kwargs) -> List[str]:
    return _base._failure_reasons(*args, **kwargs)


def _build_reasons(row: pd.Series, industry_pct: float, hot_rank, sentiment: Dict[str, object]) -> List[str]:
    return _base._build_reasons(row, industry_pct, hot_rank, sentiment)


def _build_long_term_reasons(
    row: pd.Series,
    industry_pct: float,
    sentiment: Dict[str, object],
    trend_score: float,
    liquidity_score: float,
) -> List[str]:
    return _base._build_long_term_reasons(row, industry_pct, sentiment, trend_score, liquidity_score)


def _build_tomorrow_reasons(
    row: pd.Series,
    liquidity_score: float,
    momentum_score: float,
    trend_score: float,
    historical_edge_score: float,
    execution_score: float,
    tail_setup_score: float,
    risk_penalty: float,
) -> List[str]:
    return _base._build_tomorrow_reasons(
        row,
        liquidity_score,
        momentum_score,
        trend_score,
        historical_edge_score,
        execution_score,
        tail_setup_score,
        risk_penalty,
    )


def _build_tech_potential_reasons(*args, **kwargs) -> List[str]:
    return _base._build_tech_potential_reasons(*args, **kwargs)


def _build_swing_reasons(
    row: pd.Series,
    momentum_score: float,
    trend_score: float,
    liquidity_score: float,
    risk_penalty: float,
) -> List[str]:
    return _base._build_swing_reasons(row, momentum_score, trend_score, liquidity_score, risk_penalty)


def _build_position_reasons(
    row: pd.Series,
    theme: str,
    trend_score: float,
    quality_proxy_score: float,
    liquidity_score: float,
    risk_penalty: float,
) -> List[str]:
    return _base._build_position_reasons(
        row,
        theme,
        trend_score,
        quality_proxy_score,
        liquidity_score,
        risk_penalty,
    )
