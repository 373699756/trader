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
        "limit_down_pct": 10.0,
    }


def _row_price(row: pd.Series, key: str, fallback_key: str = "price") -> float:
    return coerce_number(row.get(key)) or coerce_number(row.get(fallback_key))


def _is_sealed_limit_down(row: pd.Series, previous_close: float, limit_down_pct: float) -> bool:
    prev = coerce_number(previous_close)
    limit_pct = max(1.0, coerce_number(limit_down_pct, 10.0))
    if prev <= 0:
        return False
    open_price = _row_price(row, "open")
    high = _row_price(row, "high")
    low = _row_price(row, "low")
    close = _row_price(row, "price", "close")
    if min(open_price, high, low, close) <= 0:
        return False
    limit_price = prev * (1 - limit_pct / 100.0)
    # 日线只能近似：全天价格都贴近跌停价，视为无法按止损价卖出。
    return high <= limit_price * 1.01 and low <= limit_price * 1.005 and close <= limit_price * 1.01


def _delayed_exit_after_limit_down(
    future: pd.DataFrame,
    idx: int,
    fallback_price: float,
    reason: str,
    limit_down_pct: float,
) -> Dict[str, object]:
    previous_close = _row_price(future.iloc[idx], "price", "close") or fallback_price
    last_index = idx
    last_price = fallback_price
    for next_idx in range(idx + 1, len(future)):
        next_row = future.iloc[next_idx]
        last_index = next_idx
        last_price = _row_price(next_row, "price", "close") or last_price
        row_previous_close = coerce_number(next_row.get("prev_close")) or previous_close
        if _is_sealed_limit_down(next_row, row_previous_close, limit_down_pct):
            previous_close = last_price
            continue
        exit_price = _row_price(next_row, "open") or last_price
        return {
            "exit_price": exit_price,
            "exit_reason": "{}_limit_down_delayed".format(reason),
            "exit_index": next_idx,
            "exit_date": str(next_row.get("trade_date", "")),
        }
    return {
        "exit_price": last_price,
        "exit_reason": "{}_limit_down_unfilled".format(reason),
        "exit_index": last_index,
        "exit_date": str(future.iloc[last_index].get("trade_date", "")),
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
    limit_down_pct = max(1.0, coerce_number(policy.get("limit_down_pct"), 10.0))

    full = future.reset_index(drop=True)
    window = full.head(max_days)
    highest = entry
    exit_price = entry
    exit_reason = "hold_to_term"
    exit_index = 0
    exit_date = ""
    previous_close = entry

    for idx, row in window.iterrows():
        open_price = coerce_number(row.get("open")) or coerce_number(row.get("price"))
        high = coerce_number(row.get("high")) or coerce_number(row.get("price"))
        low = coerce_number(row.get("low")) or coerce_number(row.get("price"))
        close = coerce_number(row.get("price")) or coerce_number(row.get("close"))
        row_prev_close = coerce_number(row.get("prev_close")) or previous_close
        prior_highest = highest
        stop_price = entry * (1 - stop_loss_pct / 100.0) if stop_loss_pct > 0 else 0.0
        take_price = entry * (1 + take_profit_pct / 100.0) if take_profit_pct > 0 else 0.0
        # 日线无法判断当日高点和低点的先后顺序，移动止损只使用前一日已确认的最高价。
        trail_price = prior_highest * (1 - trailing_stop_pct / 100.0) if trailing_stop_pct > 0 else 0.0

        exit_index = idx
        exit_date = str(row.get("trade_date", ""))
        exit_price = close
        if stop_price > 0 and low > 0 and low <= stop_price:
            if _is_sealed_limit_down(row, row_prev_close, limit_down_pct):
                delayed = _delayed_exit_after_limit_down(full, idx, close, "stop_loss", limit_down_pct)
                exit_price = delayed["exit_price"]
                exit_reason = delayed["exit_reason"]
                exit_index = delayed["exit_index"]
                exit_date = delayed["exit_date"]
                break
            exit_price = open_price if 0 < open_price < stop_price else stop_price
            exit_reason = "stop_loss"
            break
        if take_price > 0 and high >= take_price:
            exit_price = take_price
            exit_reason = "take_profit"
            break
        if idx > 0 and trail_price > 0 and low > 0 and low <= trail_price:
            if _is_sealed_limit_down(row, row_prev_close, limit_down_pct):
                delayed = _delayed_exit_after_limit_down(full, idx, close, "trailing_stop", limit_down_pct)
                exit_price = delayed["exit_price"]
                exit_reason = delayed["exit_reason"]
                exit_index = delayed["exit_index"]
                exit_date = delayed["exit_date"]
                break
            exit_price = open_price if 0 < open_price < trail_price else trail_price
            exit_reason = "trailing_stop"
            break
        if high > 0:
            highest = max(highest, high)
        if close > 0:
            previous_close = close

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
