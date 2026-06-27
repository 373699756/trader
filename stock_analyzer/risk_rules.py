from typing import Dict, Optional

import pandas as pd

from . import config
from .normalization import coerce_number


def default_exit_policy(holding_days: int = 3) -> Dict[str, object]:
    """固定持有期 + 风控退出规则。

    百分比字段均以 entry_price 为基准。移动止损用入场后最高价回撤计算。
    """
    return {
        "holding_days": max(1, int(holding_days or 1)),
        "stop_loss_pct": coerce_number(getattr(config, "EXIT_STOP_LOSS_PCT", 5.0), 5.0),
        "take_profit_pct": coerce_number(getattr(config, "EXIT_TAKE_PROFIT_PCT", 8.0), 8.0),
        "trailing_stop_pct": coerce_number(getattr(config, "EXIT_TRAILING_STOP_PCT", 4.0), 4.0),
    }


def simulate_exit(
    future: pd.DataFrame,
    entry_price: float,
    holding_days: int = 3,
    policy: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    """在未来K线中模拟止损/止盈/移动止损/固定持有期退出。"""
    entry = coerce_number(entry_price)
    if future is None or future.empty or entry <= 0:
        return {"ok": False, "reason": "no_future"}

    policy = {**default_exit_policy(holding_days), **(policy or {})}
    max_days = max(1, int(policy.get("holding_days") or holding_days or 1))
    stop_loss_pct = max(0.0, coerce_number(policy.get("stop_loss_pct")))
    take_profit_pct = max(0.0, coerce_number(policy.get("take_profit_pct")))
    trailing_stop_pct = max(0.0, coerce_number(policy.get("trailing_stop_pct")))

    window = future.head(max_days).reset_index(drop=True)
    highest = entry
    exit_price = entry
    exit_reason = "hold_to_term"
    exit_index = 0
    exit_date = ""

    for idx, row in window.iterrows():
        high = coerce_number(row.get("high")) or coerce_number(row.get("price"))
        low = coerce_number(row.get("low")) or coerce_number(row.get("price"))
        close = coerce_number(row.get("price")) or coerce_number(row.get("close"))
        if high > 0:
            highest = max(highest, high)
        stop_price = entry * (1 - stop_loss_pct / 100.0) if stop_loss_pct > 0 else 0.0
        take_price = entry * (1 + take_profit_pct / 100.0) if take_profit_pct > 0 else 0.0
        trail_price = highest * (1 - trailing_stop_pct / 100.0) if trailing_stop_pct > 0 else 0.0

        exit_index = idx
        exit_date = str(row.get("trade_date", ""))
        exit_price = close
        if stop_price > 0 and low > 0 and low <= stop_price:
            exit_price = stop_price
            exit_reason = "stop_loss"
            break
        if take_price > 0 and high >= take_price:
            exit_price = take_price
            exit_reason = "take_profit"
            break
        if idx > 0 and trail_price > 0 and low > 0 and low <= trail_price:
            exit_price = trail_price
            exit_reason = "trailing_stop"
            break

    return {
        "ok": True,
        "exit_price": round(coerce_number(exit_price), 4),
        "exit_return": round((coerce_number(exit_price) / entry - 1) * 100, 4),
        "exit_reason": exit_reason,
        "exit_days": int(exit_index) + 1,
        "exit_date": exit_date,
        "holding_days": max_days,
        "policy": policy,
    }
