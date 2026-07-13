from __future__ import annotations

import math
import json
import os
import sqlite3
import random
from typing import Dict, List

from . import config
from .normalization import coerce_number
from .runtime_json import atomic_write_json
from .validation_policy import mapping_get


def average(values) -> float:
    clean = [coerce_number(value) for value in values if value is not None]
    return round(sum(clean) / len(clean), 4) if clean else 0.0


def rate(values) -> float:
    clean = list(values)
    return round(sum(1 for value in clean if value) / len(clean) * 100, 2) if clean else 0.0


def mean_confidence_interval(values, z_score: float = 1.96):
    clean = [coerce_number(value) for value in values if value is not None]
    if len(clean) < 2:
        return None, None
    mean = sum(clean) / len(clean)
    variance = sum((value - mean) ** 2 for value in clean) / (len(clean) - 1)
    margin = max(0.0, coerce_number(z_score, 1.96)) * math.sqrt(variance / len(clean))
    return round(mean - margin, 4), round(mean + margin, 4)


def block_bootstrap_mean_confidence_interval(
    values,
    samples: int = 1000,
    block_size: int = 0,
    seed: int = 20260713,
):
    """Deterministic moving-block bootstrap CI for serially dependent daily returns."""
    clean = [coerce_number(value) for value in values if value is not None]
    count = len(clean)
    if count < 2:
        return None, None
    width = max(1, min(count, int(block_size or round(math.sqrt(count)))))
    starts = list(range(max(1, count - width + 1)))
    rng = random.Random(int(seed))
    estimates = []
    for _ in range(max(200, int(samples or 1000))):
        draw = []
        while len(draw) < count:
            start = starts[rng.randrange(len(starts))]
            draw.extend(clean[start : start + width])
        estimates.append(sum(draw[:count]) / count)
    estimates.sort()
    low_index = max(0, int(len(estimates) * 0.025))
    high_index = min(len(estimates) - 1, int(len(estimates) * 0.975))
    return round(estimates[low_index], 4), round(estimates[high_index], 4)


def wilson_lower_bound(values, z_score: float = 1.96):
    clean = [bool(value) for value in values]
    if not clean:
        return None
    count = len(clean)
    successes = sum(1 for value in clean if value)
    proportion = successes / count
    z_value = max(0.0, coerce_number(z_score, 1.96))
    denominator = 1 + z_value * z_value / count
    center = proportion + z_value * z_value / (2 * count)
    spread = z_value * math.sqrt(
        (proportion * (1 - proportion) + z_value * z_value / (4 * count)) / count
    )
    return round(max(0.0, (center - spread) / denominator) * 100.0, 2)


def portfolio_max_drawdown(daily_rows: List[Dict[str, object]]) -> float:
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for row in sorted(daily_rows or [], key=lambda item: str(item.get("signal_date") or "")):
        daily_return = coerce_number(row.get("avg_primary_return_net")) / 100.0
        equity *= max(0.0, 1.0 + daily_return)
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = min(max_drawdown, (equity / peak - 1.0) * 100.0)
    return round(max_drawdown, 4)


def market_gate_outcome_summary(returns: List[float]) -> Dict[str, object]:
    clean = [coerce_number(value) for value in returns]
    avg_return = average(clean)
    win_rate = rate(value > 0 for value in clean)
    if not clean:
        actual_regime = "unknown"
    elif avg_return < 0 or win_rate < 45:
        actual_regime = "risk_off"
    elif avg_return > 0.3 and win_rate >= 55:
        actual_regime = "risk_on"
    else:
        actual_regime = "balanced"
    return {
        "outcome_sample_count": len(clean),
        "avg_primary_return_net": avg_return,
        "win_rate_primary_net": win_rate,
        "actual_regime": actual_regime,
    }


def market_gate_hit(expected_regime: str, actual_regime: str):
    expected = str(expected_regime or "").strip().lower()
    actual = str(actual_regime or "").strip().lower()
    if actual == "unknown" or expected not in {"risk_on", "balanced", "risk_off"}:
        return None
    if expected == "balanced":
        return actual == "balanced"
    return expected == actual


def deepseek_action(raw: Dict[str, object]) -> str:
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("deepseek_action") or "").strip().lower()


def has_deepseek_review(raw: Dict[str, object]) -> bool:
    if not isinstance(raw, dict):
        return False
    return any(
        key in raw
        for key in (
            "deepseek_action",
            "deepseek_veto",
            "deepseek_penalty",
            "deepseek_rank_score",
            "deepseek_score",
            "rerank_source",
        )
    )


def deepseek_covered(raw: Dict[str, object]) -> bool:
    if not isinstance(raw, dict):
        return False
    if "deepseek_covered" in raw:
        return bool(raw.get("deepseek_covered"))
    return raw.get("deepseek_score") is not None or str(raw.get("rerank_source") or "") == "deepseek"


def deepseek_avoid_or_veto(raw: Dict[str, object]) -> bool:
    if not isinstance(raw, dict):
        return False
    return bool(raw.get("deepseek_veto")) or deepseek_action(raw) == "avoid"


def deepseek_local_rank(row: Dict[str, object]) -> int:
    raw = row.get("_raw") if isinstance(row, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    return int(coerce_number(raw.get("local_rank"), 0) or 0)


def deepseek_shadow_rank(row: Dict[str, object]) -> int:
    raw = row.get("_raw") if isinstance(row, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    if raw.get("deepseek_shadow_filtered"):
        return 0
    if raw.get("deepseek_shadow_rank") is not None:
        return int(coerce_number(raw.get("deepseek_shadow_rank"), 0) or 0)
    return int(coerce_number(row.get("rank"), 0) or 0)


def deepseek_blend_alpha(raw: Dict[str, object]):
    if not isinstance(raw, dict):
        return None
    if "deepseek_blend_alpha" in raw:
        return coerce_number(raw.get("deepseek_blend_alpha"))
    if "blend_alpha" in raw:
        return coerce_number(raw.get("blend_alpha"))
    return None


def return_summary(rows: List[Dict[str, object]]) -> Dict[str, object]:
    return {
        "sample_count": len(rows),
        "avg_primary_return_net": average(row.get("_primary_return_net") for row in rows),
        "win_rate_primary_net": rate(coerce_number(row.get("_primary_return_net")) > 0 for row in rows),
    }


def deepseek_group_delta(
    priority_rows: List[Dict[str, object]],
    watch_rows: List[Dict[str, object]],
) -> Dict[str, object]:
    priority = return_summary(priority_rows)
    watch = return_summary(watch_rows)
    return {
        "priority_sample_count": priority["sample_count"],
        "watch_sample_count": watch["sample_count"],
        "priority_win_rate_primary_net": priority["win_rate_primary_net"],
        "watch_win_rate_primary_net": watch["win_rate_primary_net"],
        "win_rate_delta_pct": round(priority["win_rate_primary_net"] - watch["win_rate_primary_net"], 2),
        "avg_return_delta_pct": round(priority["avg_primary_return_net"] - watch["avg_primary_return_net"], 4),
    }


def deepseek_token_cost_summary(rows: List[Dict[str, object]]) -> Dict[str, object]:
    calls: Dict[str, Dict[str, object]] = {}
    missing_usage = 0
    for row in rows or []:
        raw = row.get("_raw") if isinstance(row, dict) else {}
        if not isinstance(raw, dict):
            raw = {}
        cost_hint = raw.get("deepseek_cost_hint") if isinstance(raw.get("deepseek_cost_hint"), dict) else {}
        usage = raw.get("deepseek_usage") if isinstance(raw.get("deepseek_usage"), dict) else {}
        usage_total_tokens = coerce_number(usage.get("total_tokens"), 0.0)
        total_tokens = coerce_number(
            cost_hint.get("total_tokens"),
            coerce_number(raw.get("deepseek_total_tokens"), usage_total_tokens),
        )
        prompt_tokens = coerce_number(
            cost_hint.get("prompt_tokens"),
            coerce_number(usage.get("prompt_tokens"), 0.0),
        )
        completion_tokens = coerce_number(
            cost_hint.get("completion_tokens"),
            coerce_number(usage.get("completion_tokens"), 0.0),
        )
        billable_tokens = coerce_number(
            cost_hint.get("billable_total_tokens"),
            coerce_number(raw.get("deepseek_billable_tokens"), total_tokens),
        )
        estimated_cost = coerce_number(cost_hint.get("estimated_cost"), 0.0)
        if total_tokens <= 0:
            missing_usage += 1
            continue
        call_id = str(raw.get("deepseek_call_id") or "").strip()
        if not call_id:
            call_id = "{}:{}:{}".format(row.get("signal_date", ""), row.get("strategy_name", ""), row.get("rank", ""))
        existing = calls.get(call_id)
        if existing and coerce_number(existing.get("total_tokens")) >= total_tokens:
            continue
        calls[call_id] = {
            "call_id": call_id,
            "source": str(raw.get("deepseek_call_source") or ""),
            "model": str(cost_hint.get("model") or ""),
            "model_tier": str(cost_hint.get("model_tier") or ""),
            "cached": bool(cost_hint.get("cached")),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "billable_total_tokens": billable_tokens,
            "estimated_cost": estimated_cost,
        }
    call_rows = list(calls.values())
    total_tokens = sum(coerce_number(item.get("total_tokens")) for item in call_rows)
    billable_tokens = sum(coerce_number(item.get("billable_total_tokens")) for item in call_rows)
    return {
        "call_count": len(call_rows),
        "total_tokens": round(total_tokens, 4),
        "billable_total_tokens": round(billable_tokens, 4),
        "estimated_cost": round(sum(coerce_number(item.get("estimated_cost")) for item in call_rows), 6),
        "missing_usage_count": missing_usage,
        "has_usage": total_tokens > 0,
        "calls": call_rows[:20],
    }


def deepseek_token_value_metrics(
    avoid_veto_rows: List[Dict[str, object]],
    counterfactual: Dict[str, object],
    token_cost: Dict[str, object],
) -> Dict[str, object]:
    skipped_loss_saved = sum(max(0.0, -coerce_number(row.get("_primary_return_net"))) for row in avoid_veto_rows)
    false_positive_loss = sum(max(0.0, coerce_number(row.get("_primary_return_net"))) for row in avoid_veto_rows)
    rerank_delta = coerce_number(counterfactual.get("avg_return_delta_pct")) * int(
        counterfactual.get("sample_count") or 0
    )
    net_value = round(skipped_loss_saved - false_positive_loss + rerank_delta, 4)
    total_tokens = coerce_number(token_cost.get("total_tokens"))
    value_per_1k = round(net_value / total_tokens * 1000.0, 4) if total_tokens > 0 else None
    return {
        "skipped_loss_saved_pct": round(skipped_loss_saved, 4),
        "false_positive_profit_loss_pct": round(false_positive_loss, 4),
        "rerank_net_return_delta_pct": coerce_number(counterfactual.get("avg_return_delta_pct")),
        "rerank_net_return_delta_points": round(rerank_delta, 4),
        "net_value_pct_points": net_value,
        "total_tokens": total_tokens,
        "billable_total_tokens": coerce_number(token_cost.get("billable_total_tokens")),
        "value_per_1k_tokens": value_per_1k,
    }


def deepseek_budget_recommendation(
    status: str,
    real_sample_count: int,
    min_real_samples: int,
    token_value: Dict[str, object],
) -> Dict[str, object]:
    value = token_value.get("value_per_1k_tokens") if isinstance(token_value, dict) else None
    if status != "ok" or int(real_sample_count or 0) < int(min_real_samples or 0):
        return {
            "action": "observe",
            "worth_expanding_budget": False,
            "reason": "样本不足，只观察，不建议扩大 DeepSeek 调用预算。",
        }
    if value is None:
        return {
            "action": "observe",
            "worth_expanding_budget": False,
            "reason": "缺少 token usage，暂不判断是否扩大预算。",
        }
    if coerce_number(value) <= 0:
        return {
            "action": "shrink",
            "worth_expanding_budget": False,
            "reason": "value_per_1k_tokens <= 0，建议收缩 DeepSeek 调用范围。",
        }
    return {
        "action": "expand",
        "worth_expanding_budget": True,
        "reason": "DeepSeek OOS 净收益/token 为正，可评估扩大预算。",
    }


def deepseek_counterfactual_topn(strategy_name: str, rows: List[Dict[str, object]]) -> Dict[str, object]:
    rows_with_local_rank = [row for row in rows if deepseek_local_rank(row) > 0]
    if not rows_with_local_rank:
        return {
            "sample_count": 0,
            "day_count": 0,
            "top_n": 0,
            "local_avg_primary_return_net": 0.0,
            "deepseek_avg_primary_return_net": 0.0,
            "avg_return_delta_pct": 0.0,
            "local_win_rate_primary_net": 0.0,
            "deepseek_win_rate_primary_net": 0.0,
            "win_rate_delta_pct": 0.0,
            "status": "missing_local_rank",
        }
    top_n = deepseek_counterfactual_n(strategy_name)
    by_date: Dict[str, List[Dict[str, object]]] = {}
    for row in rows_with_local_rank:
        by_date.setdefault(str(row.get("signal_date") or ""), []).append(row)
    local_selected: List[Dict[str, object]] = []
    deepseek_selected: List[Dict[str, object]] = []
    for date_rows in by_date.values():
        selected_rows = [
            row
            for row in date_rows
            if not row.get("_deepseek_shadow_signal") and deepseek_shadow_rank(row) > 0
        ]
        count = min(top_n, len(date_rows))
        selected_count = min(top_n, len(selected_rows))
        if count <= 0:
            continue
        local_selected.extend(
            sorted(date_rows, key=lambda item: (deepseek_local_rank(item), int(item.get("rank") or 9999)))[:count]
        )
        deepseek_selected.extend(
            sorted(
                selected_rows,
                key=lambda item: (deepseek_shadow_rank(item), deepseek_local_rank(item)),
            )[:selected_count]
        )
    local_summary = return_summary(local_selected)
    deepseek_summary = return_summary(deepseek_selected)
    return {
        "sample_count": len(deepseek_selected),
        "day_count": len(by_date),
        "top_n": top_n,
        "local_avg_primary_return_net": local_summary["avg_primary_return_net"],
        "deepseek_avg_primary_return_net": deepseek_summary["avg_primary_return_net"],
        "avg_return_delta_pct": round(
            deepseek_summary["avg_primary_return_net"] - local_summary["avg_primary_return_net"],
            4,
        ),
        "local_win_rate_primary_net": local_summary["win_rate_primary_net"],
        "deepseek_win_rate_primary_net": deepseek_summary["win_rate_primary_net"],
        "win_rate_delta_pct": round(
            deepseek_summary["win_rate_primary_net"] - local_summary["win_rate_primary_net"],
            2,
        ),
        "status": "ok" if deepseek_selected else "empty",
    }


def deepseek_counterfactual_n(strategy_name: str) -> int:
    if strategy_name == "tomorrow_picks":
        return max(1, int(getattr(config, "TOMORROW_PRIMARY_WATCH_N", 5)))
    return max(1, min(10, int(getattr(config, "RECOMMENDATION_DISPLAY_LIMIT", 18))))


def write_deepseek_attribution_snapshot(strategy_name: str, result: Dict[str, object]) -> None:
    path = str(getattr(config, "DEEPSEEK_ATTRIBUTION_PATH", ".runtime/deepseek_attribution.json") or "").strip()
    if not path:
        return
    try:
        existing = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                existing = loaded
        existing[strategy_name] = result
        atomic_write_json(path, existing, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return


def next_day_compare(rows: List[sqlite3.Row]) -> Dict[str, object]:
    return {
        "sample_count": len(rows),
        "avg_signal_to_next_open": average(row["next_open_return"] for row in rows),
        "avg_signal_to_next_close": average(row["signal_next_close_return"] for row in rows),
        "win_rate_signal_to_next_close": rate(row["signal_next_close_return"] > 0 for row in rows),
        "avg_next_open_to_close": average(row["next_close_return"] for row in rows),
        "win_rate_next_open_to_close": rate(row["next_close_return"] > 0 for row in rows),
        "avg_next_intraday_high_from_signal": average(row["signal_intraday_high_return"] for row in rows),
        "avg_next_intraday_low_from_signal": average(next_low_return_from_signal(row) for row in rows),
        "hit_3pct_rate_from_signal": rate(bool(row["signal_hit_3pct"]) for row in rows),
        "hit_5pct_rate_from_signal": rate(bool(row["signal_hit_5pct"]) for row in rows),
        "avg_trade_cost_pct": average(row["_trade_cost_pct"] for row in rows),
        "avg_signal_to_next_close_net": average(row["_primary_return_net"] for row in rows),
    }


def next_low_return_from_signal(row) -> float:
    entry = coerce_number(mapping_get(row, "price_at_signal"))
    low = coerce_number(mapping_get(row, "next_low"))
    if entry <= 0 or low <= 0:
        return 0.0
    return round((low / entry - 1.0) * 100.0, 4)


def daily_metrics(rows: List[sqlite3.Row]) -> List[Dict[str, object]]:
    grouped: Dict[str, List[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["signal_date"], []).append(row)
    daily = []
    for signal_date, items in grouped.items():
        daily.append(
            {
                "signal_date": signal_date,
                "sample_count": len(items),
                "avg_next_close_return": average(row["_next_day_return"] for row in items),
                "win_rate_next_close": rate(row["_next_day_return"] > 0 for row in items),
                "hit_3pct_rate": rate(row["_hit_3pct"] for row in items),
                "hit_5pct_rate": rate(row["_hit_5pct"] for row in items),
                "avg_hold_3d_return": average(row["_hold_3d_return"] for row in items),
                "avg_primary_return": average(row["_primary_return"] for row in items),
                "avg_primary_return_net": average(row["_primary_return_net"] for row in items),
                "win_rate_primary": rate(row["_primary_return"] > 0 for row in items),
                "win_rate_primary_net": rate(row["_primary_return_net"] > 0 for row in items),
                "avg_exit_return": average(row["_exit_return"] for row in items),
                "avg_exit_return_net": average(row["_exit_return_net"] for row in items),
                "win_rate_exit_net": rate(row["_exit_return_net"] > 0 for row in items),
                "real_sample_count": sum(1 for row in items if not row["_is_replay"]),
                "replay_sample_count": sum(1 for row in items if row["_is_replay"]),
            }
        )
    return sorted(daily, key=lambda item: item["signal_date"], reverse=True)


def top_k_sensitivity(rows: List[Dict[str, object]], top_k_values) -> Dict[str, object]:
    grouped: Dict[str, List[Dict[str, object]]] = {}
    for row in rows or []:
        grouped.setdefault(str(row.get("signal_date") or ""), []).append(row)
    reports = []
    for raw_k in top_k_values or (3, 5, 10):
        top_k = max(1, int(raw_k))
        daily = []
        incomplete_dates = []
        for signal_date, date_rows in grouped.items():
            by_rank = {
                int(coerce_number(row.get("rank"), 0)): row
                for row in date_rows
                if int(coerce_number(row.get("rank"), 0)) > 0 and row.get("_primary_ready")
            }
            expected_ranks = list(range(1, top_k + 1))
            if any(rank not in by_rank for rank in expected_ranks):
                incomplete_dates.append(signal_date)
                continue
            values = [coerce_number(by_rank[rank].get("_primary_return_net")) for rank in expected_ranks]
            daily.append(
                {
                    "signal_date": signal_date,
                    "sample_count": top_k,
                    "avg_primary_return_net": round(sum(values) / top_k, 4),
                }
            )
        daily.sort(key=lambda item: item["signal_date"], reverse=True)
        reports.append(
            {
                "top_k": top_k,
                "production": top_k == int(getattr(config, "PRODUCTION_TOP_K", 5)),
                "selection_locked": True,
                "day_count": len(daily),
                "incomplete_day_count": len(incomplete_dates),
                "avg_daily_primary_return_net": average(item["avg_primary_return_net"] for item in daily),
                "win_rate_daily_primary_net": rate(item["avg_primary_return_net"] > 0 for item in daily),
                "portfolio_max_drawdown_pct": portfolio_max_drawdown(daily),
                "daily": daily,
            }
        )
    return {
        "production_top_k": int(getattr(config, "PRODUCTION_TOP_K", 5)),
        "selection_policy": "K=5 is frozen; K=3/10 are sensitivity diagnostics and cannot be selected for production.",
        "reports": reports,
    }
