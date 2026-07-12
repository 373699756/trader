from __future__ import annotations


STRATEGY_LABELS = {
    "short_term": "盘中强势观察",
    "tomorrow_picks": "明日优先",
    "swing_picks": "2-5日持有",
}


ALPHALITE_SIGNAL_COLUMNS = (
    "ret_3d",
    "ret_5d",
    "ret_10d",
    "ret_20d",
    "ma5_gap",
    "ma20_gap",
    "vol_amount_5d",
    "breakout_20d",
    "volatility_20d",
)


__all__ = ["ALPHALITE_SIGNAL_COLUMNS", "STRATEGY_LABELS"]
