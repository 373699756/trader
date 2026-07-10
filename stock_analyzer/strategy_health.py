import json
import os
from datetime import datetime
from typing import Dict

from . import config
from .normalization import coerce_number


def strategy_status(metrics: Dict[str, object]) -> Dict[str, str]:
    metrics = metrics or {}
    sample_value = metrics.get("day_count")
    if sample_value is None:
        sample_value = metrics.get("sample_count")
    real_value = metrics.get("real_day_count")
    if real_value is None:
        real_value = metrics.get("real_sample_count")
    sample_count = int(sample_value or 0)
    real_count = int(real_value or 0)
    real_avg_return = (metrics or {}).get("real_avg_primary_return_net")
    mixed_avg_return = (metrics or {}).get("avg_primary_return_net")
    real_win_rate = (metrics or {}).get("real_win_rate_primary_net")
    mixed_win_rate = (metrics or {}).get("win_rate_primary_net")
    avg_return = coerce_number(real_avg_return if real_avg_return is not None else mixed_avg_return)
    win_rate = coerce_number(real_win_rate if real_win_rate is not None else mixed_win_rate)
    drawdown_value = metrics.get("real_avg_max_drawdown_primary")
    if drawdown_value is None:
        drawdown_value = metrics.get("avg_max_drawdown_primary", metrics.get("avg_max_drawdown_3d"))
    drawdown = coerce_number(drawdown_value)
    min_real = int(
        getattr(
            config,
            "STRATEGY_DECAY_MIN_REAL_DAYS",
            getattr(config, "STRATEGY_DECAY_MIN_REAL_SAMPLES", 20),
        )
    )
    retire_winrate = coerce_number(getattr(config, "STRATEGY_RETIRE_WINRATE", 48.0), 48.0)
    drawdown_floor = coerce_number(
        getattr(config, "STRATEGY_VALIDATION_MAX_AVG_DRAWDOWN_PCT", -8.0),
        -8.0,
    )

    if real_count < 10 and sample_count < 30:
        return {
            "state": "pending",
            "level": "pending",
            "label": "样本不足",
            "advice": "真实样本不足，回放只能粗筛；先保存并更新前瞻验证。",
        }
    if real_count < min_real:
        return {
            "state": "probation",
            "level": "pending",
            "label": "真实样本少",
            "advice": "回放样本已补足但真实样本不足，不能高权重采信。",
        }
    if win_rate < retire_winrate or avg_return <= 0 or drawdown <= drawdown_floor:
        return {
            "state": "retired",
            "level": "bad",
            "label": "自动退场",
            "advice": "真实交易日主周期净胜率、净收益或回撤跌破退场阈值，暂停执行。",
        }
    if avg_return > 0.5 and win_rate >= 52 and drawdown > -8:
        return {
            "state": "active",
            "level": "good",
            "label": "继续观察",
            "advice": "真实样本主周期净表现尚可，但仍需控制仓位。",
        }
    return {
        "state": "probation",
        "level": "neutral",
        "label": "观察降权",
        "advice": "表现不突出，强共识中降低权重并继续与其他策略对比。",
    }


def save_strategy_status(status_by_strategy: Dict[str, Dict[str, object]], path: str = "") -> None:
    target = path or getattr(config, "STRATEGY_STATUS_PATH", ".runtime/strategy_status.json")
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "strategies": status_by_strategy,
    }
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def load_strategy_status(path: str = "") -> Dict[str, Dict[str, object]]:
    target = path or getattr(config, "STRATEGY_STATUS_PATH", ".runtime/strategy_status.json")
    try:
        with open(target, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        strategies = payload.get("strategies") or {}
        return strategies if isinstance(strategies, dict) else {}
    except Exception:
        return {}
