from __future__ import annotations

import json
import sqlite3
from typing import Dict, Optional

from . import config
from .normalization import coerce_number
from .risk_rules import simulate_exit


def compute_stance_outcome(provider, snapshot: sqlite3.Row) -> Optional[Dict[str, object]]:
    try:
        history = provider.get_history(snapshot["code"], days=180)
    except Exception:
        return None
    if history is None or history.empty or "trade_date" not in history.columns:
        return None
    frame = history.sort_values("trade_date").reset_index(drop=True)
    signal_date = str(snapshot["prediction_date"]).replace("-", "")
    future = frame[frame["trade_date"].astype(str).str.replace("-", "", regex=False) > signal_date].reset_index(
        drop=True
    )
    if future.empty:
        return None
    first = future.iloc[0]
    entry = coerce_number(first.get("open")) or coerce_number(first.get("price"))
    if entry <= 0:
        entry = coerce_number(snapshot["price_at_signal"])
    close = coerce_number(first.get("price")) or coerce_number(first.get("close"))
    if entry <= 0 or close <= 0:
        return None
    try:
        optimization = json.loads(snapshot["optimization_json"] or "{}")
    except Exception:
        optimization = {}
    holding_days = max(1, int(getattr(config, "STANCE_TRACKING_HOLDING_DAYS", 5)))
    exit_result = simulate_exit(
        future,
        entry,
        holding_days=holding_days,
        policy=stance_exit_policy(optimization, holding_days),
    )
    return {
        "next_trade_date": str(first.get("trade_date")),
        "future_days": len(future),
        "next_open": round(entry, 4),
        "next_close": round(close, 4),
        "next_close_return": round((close / entry - 1) * 100, 4),
        "exit_return": coerce_number(exit_result.get("exit_return")),
        "exit_reason": str(exit_result.get("exit_reason") or ""),
        "exit_days": int(exit_result.get("exit_days") or 0),
        "exit_date": str(exit_result.get("exit_date") or ""),
    }


def stance_exit_policy(optimization: Dict[str, object], holding_days: int) -> Dict[str, object]:
    policy = {"holding_days": holding_days}
    for source_key, target_key in (
        ("stop_loss_pct", "stop_loss_pct"),
        ("take_profit_pct", "take_profit_pct"),
        ("trailing_stop_pct", "trailing_stop_pct"),
    ):
        value = optimization.get(source_key) if isinstance(optimization, dict) else None
        number = coerce_number(value, 0.0)
        if number > 0:
            policy[target_key] = number
    return policy
