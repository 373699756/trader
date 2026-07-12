from __future__ import annotations

from typing import Dict, List, Tuple

import pandas as pd


__all__ = [
    "score_breakout_candidates",
    "score_position_candidates",
    "score_reversal_candidates",
    "score_smallcap_value_candidates",
    "_unsupported_retired_strategy",
]


def _unsupported_retired_strategy(strategy_name: str):
    raise ValueError(
        "{} 已下线；当前只支持 short_term、tomorrow_picks、swing_picks".format(strategy_name)
    )


def score_position_candidates(
    df: pd.DataFrame,
    top_n: int = 30,
    market_filter: str = "all",
    market_regime: Dict[str, object] = None,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    _unsupported_retired_strategy("position_picks")


def score_reversal_candidates(
    df: pd.DataFrame,
    top_n: int = 30,
    market_filter: str = "all",
    market_regime: Dict[str, object] = None,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    _unsupported_retired_strategy("reversal_picks")


def score_smallcap_value_candidates(
    df: pd.DataFrame,
    top_n: int = 30,
    market_filter: str = "all",
    market_regime: Dict[str, object] = None,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    _unsupported_retired_strategy("smallcap_value_picks")


def score_breakout_candidates(
    df: pd.DataFrame,
    top_n: int = 30,
    market_filter: str = "all",
    market_regime: Dict[str, object] = None,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    _unsupported_retired_strategy("breakout_picks")
