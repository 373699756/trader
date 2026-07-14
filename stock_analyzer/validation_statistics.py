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


