import json
import os
from datetime import datetime
from typing import Dict

from . import config
from .normalization import coerce_number


def strategy_status(metrics: Dict[str, object]) -> Dict[str, str]:
    sample_count = int((metrics or {}).get("sample_count") or 0)
    real_count = int((metrics or {}).get("real_sample_count") or 0)
    avg_return = coerce_number((metrics or {}).get("real_avg_primary_return_net") or (metrics or {}).get("avg_primary_return_net"))
    win_rate = coerce_number((metrics or {}).get("real_win_rate_primary_net") or (metrics or {}).get("win_rate_primary_net"))
    drawdown = coerce_number((metrics or {}).get("avg_max_drawdown_3d"))
    min_real = int(getattr(config, "STRATEGY_DECAY_MIN_REAL_SAMPLES", 20))
    retire_winrate = coerce_number(getattr(config, "STRATEGY_RETIRE_WINRATE", 48.0), 48.0)

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
    if win_rate < retire_winrate or avg_return < 0:
        return {
            "state": "retired",
            "level": "bad",
            "label": "自动退场",
            "advice": "真实样本主周期净胜率或净收益跌破退场阈值，强共识中暂停采信。",
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
