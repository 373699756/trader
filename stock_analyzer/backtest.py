from typing import Dict, Iterable, List

import pandas as pd

from . import config
from .factors import compute_alphalite_for_stock
from .normalization import coerce_number, normalize_code, rename_known_columns
from .risk_rules import simulate_exit


# AlphaLite 信号权重。calibrate.py 离线扫描后写入 .runtime/weights.json 的
# "alphalite_signal" 段即可覆盖，无需改代码。
_DEFAULT_ALPHALITE_WEIGHTS = {
    "ret_5d": 0.20,
    "ret_10d": 0.22,
    "ret_20d": 0.25,
    "ma20_gap": 0.14,
    "vol_amount_5d": 2.0,
    "breakout_20d": 4.0,
    "volatility_20d": -0.35,
}


def _load_alphalite_weights() -> Dict[str, float]:
    import copy
    import json
    import os

    weights = copy.deepcopy(_DEFAULT_ALPHALITE_WEIGHTS)
    path = getattr(config, "WEIGHTS_OVERRIDE_PATH", os.path.join(".runtime", "weights.json"))
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            override = payload.get("alphalite_signal") or {}
            for key, value in override.items():
                if key in weights:
                    weights[key] = float(value)
    except Exception:
        return copy.deepcopy(_DEFAULT_ALPHALITE_WEIGHTS)
    return weights


ALPHALITE_SIGNAL_WEIGHTS = _load_alphalite_weights()


def run_alphalite_backtest(
    history_by_code: Dict[str, pd.DataFrame],
    top_k: int = 10,
    holding_days: int = 3,
    cost_rate: float = 0.0015,
) -> Dict[str, object]:
    rows = []
    for code, history in history_by_code.items():
        prepared = _prepare_history(code, history)
        if len(prepared) < holding_days + 25:
            continue
        factor = compute_alphalite_for_stock(code, prepared.iloc[: -holding_days])
        if not factor:
            continue
        entry_price = prepared["price"].iloc[-holding_days - 1]
        fixed_exit_price = prepared["price"].iloc[-1]
        if entry_price <= 0 or fixed_exit_price <= 0:
            continue
        future = prepared.iloc[-holding_days:]
        exit_result = simulate_exit(future, entry_price, holding_days=holding_days)
        gross_return = exit_result.get("exit_return", 0.0) if exit_result.get("ok") else (
            fixed_exit_price / entry_price - 1
        ) * 100
        fixed_gross_return = (fixed_exit_price / entry_price - 1) * 100
        net_return = gross_return - cost_rate * 100
        rows.append(
            {
                "code": normalize_code(code),
                "signal": round(_alphalite_signal(factor), 4),
                "gross_return": round(gross_return, 4),
                "fixed_gross_return": round(fixed_gross_return, 4),
                "net_return": round(net_return, 4),
                "exit_reason": exit_result.get("exit_reason", "hold_to_term"),
                "exit_days": exit_result.get("exit_days", holding_days),
                "exit_date": exit_result.get("exit_date", _trade_date(prepared, len(prepared) - 1)),
                "factor": factor,
            }
        )
    rows.sort(key=lambda item: item["signal"], reverse=True)
    selected = rows[:top_k]
    if not selected:
        return {
            "ok": False,
            "error": "没有足够历史数据可回测",
            "selected": [],
            "metrics": {},
        }
    returns = [item["net_return"] for item in selected]
    win_count = sum(1 for value in returns if value > 0)
    metrics = {
        "sample_count": len(rows),
        "selected_count": len(selected),
        "top_k": top_k,
        "holding_days": holding_days,
        "cost_rate": cost_rate,
        "avg_net_return": round(sum(returns) / len(returns), 4),
        "win_rate": round(win_count / len(returns) * 100, 2),
        "best_return": round(max(returns), 4),
        "worst_return": round(min(returns), 4),
    }
    return {"ok": True, "selected": selected, "metrics": metrics}


def run_rolling_alphalite_backtest(
    history_by_code: Dict[str, pd.DataFrame],
    top_k: int = 10,
    holding_days: int = 3,
    lookback_days: int = 30,
    rebalance_step: int = 1,
    cost_rate: float = 0.0015,
    weights: Dict[str, float] = None,
) -> Dict[str, object]:
    prepared = {
        normalize_code(code): _prepare_history(code, history)
        for code, history in history_by_code.items()
        if history is not None and not history.empty
    }
    prepared = {code: df for code, df in prepared.items() if len(df) >= lookback_days + holding_days + 5}
    if not prepared:
        return {"ok": False, "error": "没有足够历史数据可滚动回测", "trades": [], "metrics": {}}

    min_len = min(len(df) for df in prepared.values())
    max_index = min_len - holding_days
    start_index = max(lookback_days, min_len - 80)
    trades = []
    equity_curve = []
    equity = 1.0

    for signal_index in range(start_index, max_index, max(1, rebalance_step)):
        signals = []
        for code, history in prepared.items():
            window = history.iloc[: signal_index + 1]
            if len(window) < lookback_days:
                continue
            factor = compute_alphalite_for_stock(code, window.tail(max(lookback_days, 25)))
            if not factor:
                continue
            entry_price = history["price"].iloc[signal_index]
            fixed_exit_price = history["price"].iloc[signal_index + holding_days]
            if entry_price <= 0 or fixed_exit_price <= 0:
                continue
            future = history.iloc[signal_index + 1 : signal_index + holding_days + 1]
            exit_result = simulate_exit(future, entry_price, holding_days=holding_days)
            gross_return = exit_result.get("exit_return", 0.0) if exit_result.get("ok") else (
                fixed_exit_price / entry_price - 1
            ) * 100
            fixed_gross_return = (fixed_exit_price / entry_price - 1) * 100
            net_return = gross_return - cost_rate * 100
            signals.append(
                {
                    "code": code,
                    "signal": _alphalite_signal(factor, weights),
                    "net_return": net_return,
                    "gross_return": gross_return,
                    "fixed_gross_return": fixed_gross_return,
                    "trade_date": _trade_date(history, signal_index),
                    "exit_date": exit_result.get("exit_date") or _trade_date(history, signal_index + holding_days),
                    "exit_reason": exit_result.get("exit_reason", "hold_to_term"),
                    "exit_days": exit_result.get("exit_days", holding_days),
                }
            )
        signals.sort(key=lambda item: item["signal"], reverse=True)
        selected = signals[:top_k]
        if not selected:
            continue
        period_return = sum(item["net_return"] for item in selected) / len(selected)
        equity *= 1 + period_return / 100
        equity_curve.append({"date": selected[0]["exit_date"], "equity": round(equity, 6)})
        trades.append(
            {
                "trade_date": selected[0]["trade_date"],
                "exit_date": selected[0]["exit_date"],
                "period_return": round(period_return, 4),
                "selected": selected,
            }
        )

    if not trades:
        return {"ok": False, "error": "没有产生有效回测交易", "trades": [], "metrics": {}}

    period_returns = [trade["period_return"] for trade in trades]
    win_count = sum(1 for value in period_returns if value > 0)
    metrics = {
        "sample_count": len(prepared),
        "period_count": len(trades),
        "top_k": top_k,
        "holding_days": holding_days,
        "lookback_days": lookback_days,
        "rebalance_step": rebalance_step,
        "cost_rate": cost_rate,
        "avg_period_return": round(sum(period_returns) / len(period_returns), 4),
        "win_rate": round(win_count / len(period_returns) * 100, 2),
        "total_return": round((equity - 1) * 100, 4),
        "max_drawdown": round(_max_drawdown([point["equity"] for point in equity_curve]) * 100, 4),
        "best_period": round(max(period_returns), 4),
        "worst_period": round(min(period_returns), 4),
    }
    return {
        "ok": True,
        "metrics": metrics,
        "equity_curve": equity_curve,
        "trades": trades[-20:],
    }


def parse_code_list(value: str, default_codes: Iterable[str] = ()) -> List[str]:
    if value:
        raw_codes = value.replace("，", ",").replace(" ", ",").split(",")
    else:
        raw_codes = list(default_codes)
    codes = []
    for raw in raw_codes:
        text = str(raw).strip()
        if not text:
            continue
        codes.append(normalize_code(text))
    return list(dict.fromkeys(codes))


def _prepare_history(code: str, history: pd.DataFrame) -> pd.DataFrame:
    df = rename_known_columns(history.copy())
    if "code" not in df.columns:
        df["code"] = code
    if "price" not in df.columns and "close" in df.columns:
        df["price"] = df["close"]
    if "price" not in df.columns:
        df["price"] = 0.0
    for column in ("price", "high", "low", "turnover"):
        if column not in df.columns:
            df[column] = 0.0
        df[column] = df[column].map(coerce_number)
    return df.reset_index(drop=True)


def _alphalite_signal(factor: Dict[str, float], weights: Dict[str, float] = None) -> float:
    w = weights or ALPHALITE_SIGNAL_WEIGHTS
    return (
        coerce_number(factor.get("ret_5d")) * w["ret_5d"]
        + coerce_number(factor.get("ret_10d")) * w["ret_10d"]
        + coerce_number(factor.get("ret_20d")) * w["ret_20d"]
        + coerce_number(factor.get("ma20_gap")) * w["ma20_gap"]
        + coerce_number(factor.get("vol_amount_5d")) * w["vol_amount_5d"]
        + coerce_number(factor.get("breakout_20d")) * w["breakout_20d"]
        + coerce_number(factor.get("volatility_20d")) * w["volatility_20d"]
    )


def _trade_date(history: pd.DataFrame, index: int) -> str:
    if "trade_date" in history.columns:
        return str(history["trade_date"].iloc[index])
    return str(index)


def _max_drawdown(equity_values: List[float]) -> float:
    peak = 0.0
    max_dd = 0.0
    for value in equity_values:
        peak = max(peak, value)
        if peak <= 0:
            continue
        max_dd = max(max_dd, (peak - value) / peak)
    return max_dd
