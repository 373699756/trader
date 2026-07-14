from __future__ import annotations

from typing import Dict, Iterable, List

import pandas as pd

from .normalization import coerce_number
from .risk_rules import simulate_exit


MAX_EXIT_CANDIDATES = 5


def exit_policy_candidates(strategy_name: str) -> List[Dict[str, object]]:
    strategy = str(strategy_name or "")
    if strategy == "tomorrow_picks":
        return [
            _candidate("current_8_5_4", 1, 8.0, 5.0, 4.0),
            _candidate("protective_stop_t1_close", 1, 0.0, 5.0, 0.0),
            _candidate("vol_norm_stop_t1_close", 1, 0.0, 0.0, 0.0, stop_mode="volatility_normalized"),
            _candidate("no_fixed_take_profit", 1, 0.0, 5.0, 4.0),
        ]
    if strategy == "swing_picks":
        return [
            _candidate("current_8_5_4", 5, 8.0, 5.0, 4.0, take_profit_earliest_offset_days=1),
            _candidate("vol_norm_stop_time_stop", 5, 0.0, 0.0, 0.0, stop_mode="volatility_normalized"),
            _candidate("fixed_stop_dynamic_trailing", 5, 0.0, 5.0, 4.0),
        ]
    return []


def evaluate_exit_policy_candidates(
    strategy_name: str,
    samples: Iterable[Dict[str, object]],
    *,
    top_k: int = 5,
) -> Dict[str, object]:
    candidates = exit_policy_candidates(strategy_name)[:MAX_EXIT_CANDIDATES]
    rows = [dict(row) for row in samples or [] if isinstance(row, dict)]
    results = []
    for candidate in candidates:
        evaluated = _evaluate_candidate(candidate, rows, top_k=max(1, int(top_k or 5)))
        results.append(evaluated)
    return {
        "ok": bool(results),
        "strategy": str(strategy_name or ""),
        "experiment_family": "p3_exit_policy_candidates",
        "candidate_count": len(results),
        "max_candidates": MAX_EXIT_CANDIDATES,
        "multiple_testing_trials": len(results),
        "conservative_intraday_order": "stop_first_when_daily_bar_hits_stop_and_take_profit",
        "results": results,
    }


def _candidate(
    candidate_id: str,
    holding_days: int,
    take_profit_pct: float,
    stop_loss_pct: float,
    trailing_stop_pct: float,
    **extra,
) -> Dict[str, object]:
    policy = {
        "holding_days": int(holding_days),
        "take_profit_pct": float(take_profit_pct),
        "stop_loss_pct": float(stop_loss_pct),
        "trailing_stop_pct": float(trailing_stop_pct),
        "limit_down_pct": 10.0,
        **extra,
    }
    return {"candidate_id": candidate_id, "policy": policy}


def _evaluate_candidate(candidate: Dict[str, object], samples: List[Dict[str, object]], top_k: int) -> Dict[str, object]:
    policy = dict(candidate.get("policy") or {})
    daily_returns: Dict[str, List[float]] = {}
    skipped = 0
    for sample in samples:
        signal_date = str(sample.get("signal_date") or "")
        entry_price = coerce_number(sample.get("entry_price"), None)
        if entry_price is None:
            entry_price = coerce_number(sample.get("price_at_signal"), None)
        future = _future_frame(sample)
        if not signal_date or entry_price is None or entry_price <= 0 or future.empty:
            skipped += 1
            continue
        resolved_policy = _resolve_policy(policy, sample)
        result = simulate_exit(
            future,
            entry_price,
            holding_days=int(resolved_policy.get("holding_days") or 1),
            policy=resolved_policy,
        )
        if not result.get("ok"):
            skipped += 1
            continue
        daily_returns.setdefault(signal_date, []).append(coerce_number(result.get("exit_return")))
    portfolio_returns = []
    for signal_date, values in sorted(daily_returns.items()):
        selected = values[:top_k]
        if selected:
            portfolio_returns.append({"signal_date": signal_date, "net_return": _avg(selected)})
    return {
        "candidate_id": str(candidate.get("candidate_id") or ""),
        "policy": policy,
        "sample_count": sum(len(values) for values in daily_returns.values()),
        "skipped": skipped,
        "day_count": len(portfolio_returns),
        "avg_portfolio_return": round(_avg([item["net_return"] for item in portfolio_returns]), 4),
        "portfolio_returns": portfolio_returns,
        "status": "ok" if portfolio_returns else "insufficient_samples",
    }


def _resolve_policy(policy: Dict[str, object], sample: Dict[str, object]) -> Dict[str, object]:
    resolved = dict(policy)
    if str(resolved.get("stop_mode") or "") == "volatility_normalized":
        volatility = coerce_number(sample.get("volatility_20d"), None)
        raw = sample.get("raw") if isinstance(sample.get("raw"), dict) else {}
        if volatility is None:
            volatility = coerce_number(raw.get("volatility_20d"), 0.0) if isinstance(raw, dict) else 0.0
        resolved["stop_loss_pct"] = max(2.5, min(8.0, volatility * 0.8 if volatility else 5.0))
    return resolved


def _future_frame(sample: Dict[str, object]) -> pd.DataFrame:
    raw = sample.get("raw_prices")
    if raw is None:
        raw = sample.get("future_prices")
    if raw is None and isinstance(sample.get("raw"), dict):
        raw = sample["raw"].get("raw_prices") or sample["raw"].get("future_prices")
    if isinstance(raw, pd.DataFrame):
        return raw.copy()
    if isinstance(raw, list):
        return pd.DataFrame([row for row in raw if isinstance(row, dict)])
    return pd.DataFrame()


def _avg(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0
