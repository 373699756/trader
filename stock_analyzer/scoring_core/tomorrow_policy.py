from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Tuple

import pandas as pd

from . import base as _base


__all__ = [
    "_market_regime_with_history",
    "_parse_datetime_value",
    "_time_parts",
    "_tomorrow_analysis_window",
    "_tomorrow_backup_reject",
    "_tomorrow_backup_rows",
    "_tomorrow_display_gate",
    "_tomorrow_hard_reject",
    "_tomorrow_historical_edge_score",
    "_tomorrow_intraday_relaxed_mode",
    "_tomorrow_policy",
    "_tomorrow_primary_eligibility",
    "_tomorrow_primary_watch_limit",
    "_tomorrow_quote_time",
    "_tomorrow_risk_penalty",
    "_tomorrow_risk_penalty_parts",
]


def _tomorrow_policy() -> Dict[str, object]:
    return _base._tomorrow_policy()


def _tomorrow_hard_reject(row: pd.Series, intraday_relaxed: bool = False) -> bool:
    return _base._tomorrow_hard_reject(row, intraday_relaxed=intraday_relaxed)


def _tomorrow_historical_edge_score(row: pd.Series, context: Dict[str, List[float]]) -> float:
    return _base._tomorrow_historical_edge_score(row, context)


def _tomorrow_backup_reject(row: pd.Series) -> bool:
    return _base._tomorrow_backup_reject(row)


def _tomorrow_backup_rows(
    df: pd.DataFrame,
    context: Dict[str, List[float]],
    market_regime: Dict[str, object] = None,
    provisional: bool = False,
) -> List[Dict[str, object]]:
    return _base._tomorrow_backup_rows(
        df,
        context,
        market_regime=market_regime,
        provisional=provisional,
    )


def _tomorrow_display_gate(
    top_n: int,
    market_regime: Dict[str, object] = None,
    intraday_relaxed: bool = False,
) -> Tuple[int, float, str]:
    return _base._tomorrow_display_gate(top_n, market_regime, intraday_relaxed=intraday_relaxed)


def _market_regime_with_history(market_regime: Dict[str, object], df: pd.DataFrame) -> Dict[str, object]:
    return _base._market_regime_with_history(market_regime, df)


def _tomorrow_primary_watch_limit(strict_count: int, market_regime: Dict[str, object] = None) -> int:
    return _base._tomorrow_primary_watch_limit(strict_count, market_regime=market_regime)


def _tomorrow_primary_eligibility(row: Dict[str, object], gate_min_score: float) -> Tuple[bool, List[str]]:
    return _base._tomorrow_primary_eligibility(row, gate_min_score)


def _tomorrow_risk_penalty(row: pd.Series) -> float:
    return _base._tomorrow_risk_penalty(row)


def _tomorrow_risk_penalty_parts(row: pd.Series, provisional: bool = False) -> Dict[str, float]:
    return _base._tomorrow_risk_penalty_parts(row, provisional=provisional)


def _tomorrow_analysis_window() -> str:
    return _base._tomorrow_analysis_window()


def _tomorrow_intraday_relaxed_mode(now: datetime = None, quote_time: datetime = None) -> bool:
    return _base._tomorrow_intraday_relaxed_mode(now=now, quote_time=quote_time)


def _tomorrow_quote_time(df: pd.DataFrame) -> datetime:
    return _base._tomorrow_quote_time(df)


def _parse_datetime_value(value) -> datetime:
    return _base._parse_datetime_value(value)


def _time_parts(value: str, fallback: Tuple[int, int]) -> Tuple[int, int]:
    return _base._time_parts(value, fallback)
