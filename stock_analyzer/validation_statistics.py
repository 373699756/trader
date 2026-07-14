from __future__ import annotations

import math
import json
import os
import sqlite3
import random
import statistics
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
    estimates = _moving_block_bootstrap_means(
        clean,
        samples=samples,
        block_size=block_size,
        seed=seed,
    )
    estimates.sort()
    low_index = max(0, int(len(estimates) * 0.025))
    high_index = min(len(estimates) - 1, int(len(estimates) * 0.975))
    return round(estimates[low_index], 4), round(estimates[high_index], 4)


def paired_increment_statistics(
    baseline_values,
    challenger_values,
    *,
    samples: int = 2000,
    block_size: int = 0,
    seed: int = 20260714,
    trial_count: int = 1,
    dsr_probability: float = 0.95,
) -> Dict[str, object]:
    """Evaluate challenger-minus-baseline returns as one paired daily time series."""
    pairs = []
    for baseline, challenger in zip(baseline_values or [], challenger_values or []):
        if baseline is None or challenger is None:
            continue
        pairs.append((coerce_number(baseline), coerce_number(challenger)))
    increments = [challenger - baseline for baseline, challenger in pairs]
    ci_low, ci_high = block_bootstrap_mean_confidence_interval(
        increments,
        samples=samples,
        block_size=block_size,
        seed=seed,
    )
    p_value = moving_block_bootstrap_positive_mean_p_value(
        increments,
        samples=samples,
        block_size=block_size,
        seed=seed,
    )
    dsr = deflated_sharpe_ratio(
        increments,
        trial_count=trial_count,
        probability_threshold=dsr_probability,
    )
    mean_increment = sum(increments) / len(increments) if increments else None
    return {
        "method": "paired_daily_moving_block_bootstrap",
        "sample_count": len(increments),
        "baseline_mean_return_pct": (
            round(sum(item[0] for item in pairs) / len(pairs), 6) if pairs else None
        ),
        "challenger_mean_return_pct": (
            round(sum(item[1] for item in pairs) / len(pairs), 6) if pairs else None
        ),
        "mean_incremental_return_pct": (
            round(mean_increment, 6) if mean_increment is not None else None
        ),
        "increment_ci95_low": ci_low,
        "increment_ci95_high": ci_high,
        "p_value": p_value,
        "block_size": _resolved_block_size(len(increments), block_size),
        "bootstrap_samples": max(200, int(samples or 2000)),
        "incremental_returns_pct": [round(value, 6) for value in increments],
        "positive_day_count": sum(1 for value in increments if value > 0),
        "dsr": dsr,
        "economic_increment_passed": mean_increment is not None and mean_increment > 0,
        "confidence_interval_passed": ci_low is not None and ci_low >= 0,
    }


def moving_block_bootstrap_positive_mean_p_value(
    values,
    *,
    samples: int = 2000,
    block_size: int = 0,
    seed: int = 20260714,
):
    """One-sided null probability for a positive mean, preserving serial blocks."""
    clean = [coerce_number(value) for value in values or [] if value is not None]
    if len(clean) < 2:
        return None
    observed = sum(clean) / len(clean)
    if observed <= 0:
        return 1.0
    centered = [value - observed for value in clean]
    estimates = _moving_block_bootstrap_means(
        centered,
        samples=samples,
        block_size=block_size,
        seed=seed,
    )
    exceedances = sum(1 for estimate in estimates if estimate >= observed)
    return round((exceedances + 1.0) / (len(estimates) + 1.0), 8)


def benjamini_hochberg_fdr(p_values: List[object], q: float = 0.1) -> Dict[str, object]:
    valid = []
    adjusted = [None] * len(p_values or [])
    for index, value in enumerate(p_values or []):
        p_value = coerce_number(value, None)
        if p_value is None or p_value < 0 or p_value > 1:
            continue
        valid.append((p_value, index))
    q = max(0.0, min(1.0, coerce_number(q, 0.1)))
    if not valid:
        return {
            "method": "benjamini_hochberg",
            "q": q,
            "tested_count": 0,
            "rejected": [],
            "rejected_count": 0,
            "threshold": None,
            "adjusted_p_values": adjusted,
        }
    valid.sort(key=lambda item: item[0])
    tested_count = len(valid)
    rejected_rank = 0
    threshold = None
    for rank, (p_value, _original_index) in enumerate(valid, start=1):
        candidate_threshold = q * rank / tested_count
        if p_value <= candidate_threshold:
            threshold = candidate_threshold
            rejected_rank = rank
    running_min = 1.0
    for rank in range(tested_count, 0, -1):
        p_value, original_index = valid[rank - 1]
        running_min = min(running_min, p_value * tested_count / rank)
        adjusted[original_index] = round(min(1.0, running_min), 8)
    rejected = [original_index for _p_value, original_index in valid[:rejected_rank]]
    return {
        "method": "benjamini_hochberg",
        "q": q,
        "tested_count": tested_count,
        "rejected": rejected,
        "rejected_count": len(rejected),
        "threshold": round(threshold, 8) if threshold is not None else None,
        "adjusted_p_values": adjusted,
    }


def unified_experiment_fdr(
    experiments: List[Dict[str, object]],
    *,
    q: float = 0.1,
    experiment_family: str = "",
) -> Dict[str, object]:
    """Apply one BH correction to every declared trial in an experiment family."""
    tested = []
    for record in experiments or []:
        if not isinstance(record, dict):
            continue
        family = str(record.get("experiment_family") or "")
        if experiment_family and family != experiment_family:
            continue
        experiment_id = str(record.get("experiment_id") or "").strip()
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        raw_p_values = result.get("p_values")
        if not isinstance(raw_p_values, list):
            single = result.get("p_value", record.get("p_value"))
            raw_p_values = [] if single is None else [single]
        declared = max(
            1,
            int(coerce_number(record.get("trial_count"), len(raw_p_values) or 1)),
        )
        for index in range(declared):
            p_value = raw_p_values[index] if index < len(raw_p_values) else 1.0
            tested.append(
                {
                    "experiment_id": experiment_id,
                    "experiment_family": family,
                    "trial_index": index,
                    "p_value": coerce_number(p_value, 1.0),
                    "reported": index < len(raw_p_values),
                }
            )
    correction = benjamini_hochberg_fdr(
        [item["p_value"] for item in tested],
        q=q,
    )
    rejected_indexes = set(correction.get("rejected") or [])
    for index, item in enumerate(tested):
        item["adjusted_p_value"] = (correction.get("adjusted_p_values") or [None] * len(tested))[index]
        item["rejected"] = index in rejected_indexes
    return {
        **correction,
        "scope": "full_experiment_family",
        "experiment_family": str(experiment_family or "all"),
        "declared_trial_count": len(tested),
        "reported_trial_count": sum(1 for item in tested if item["reported"]),
        "rejected_experiment_ids": sorted(
            {item["experiment_id"] for item in tested if item["rejected"] and item["experiment_id"]}
        ),
        "trials": tested,
    }


def deflated_sharpe_ratio(
    returns,
    *,
    trial_count: int = 1,
    probability_threshold: float = 0.95,
) -> Dict[str, object]:
    """Approximate DSR using non-normal Sharpe variance and the number of trials."""
    clean = [coerce_number(value) for value in returns or [] if value is not None]
    count = len(clean)
    threshold = max(0.0, min(1.0, coerce_number(probability_threshold, 0.95)))
    trials = max(1, int(trial_count or 1))
    if count < 3:
        return {
            "method": "deflated_sharpe_ratio",
            "status": "insufficient_samples",
            "sample_count": count,
            "trial_count": trials,
            "probability": None,
            "probability_threshold": threshold,
            "passed": False,
        }
    mean = statistics.fmean(clean)
    stdev = statistics.stdev(clean)
    if stdev <= 0:
        probability = 1.0 if mean > 0 else 0.0
        return {
            "method": "deflated_sharpe_ratio",
            "status": "degenerate_variance",
            "sample_count": count,
            "trial_count": trials,
            "observed_daily_sharpe": None,
            "expected_max_daily_sharpe": 0.0,
            "probability": probability,
            "probability_threshold": threshold,
            "passed": probability >= threshold,
        }
    sharpe = mean / stdev
    centered = [(value - mean) / stdev for value in clean]
    skewness = sum(value ** 3 for value in centered) / count
    kurtosis = sum(value ** 4 for value in centered) / count
    normal = statistics.NormalDist()
    expected_max = 0.0
    if trials > 1:
        euler_gamma = 0.5772156649015329
        first = normal.inv_cdf(1.0 - 1.0 / trials)
        second = normal.inv_cdf(1.0 - 1.0 / (trials * math.e))
        expected_max = ((1.0 - euler_gamma) * first + euler_gamma * second) / math.sqrt(count - 1)
    variance_term = max(
        1e-12,
        1.0 - skewness * sharpe + ((kurtosis - 1.0) / 4.0) * sharpe * sharpe,
    )
    z_score = (sharpe - expected_max) * math.sqrt(count - 1) / math.sqrt(variance_term)
    probability = normal.cdf(z_score)
    return {
        "method": "deflated_sharpe_ratio",
        "status": "ready",
        "sample_count": count,
        "trial_count": trials,
        "observed_daily_sharpe": round(sharpe, 8),
        "observed_annualized_sharpe": round(sharpe * math.sqrt(252.0), 6),
        "expected_max_daily_sharpe": round(expected_max, 8),
        "skewness": round(skewness, 8),
        "kurtosis": round(kurtosis, 8),
        "z_score": round(z_score, 8),
        "probability": round(probability, 8),
        "probability_threshold": threshold,
        "passed": probability >= threshold,
    }


def _resolved_block_size(count: int, block_size: int) -> int:
    if count <= 0:
        return 0
    return max(1, min(count, int(block_size or round(math.sqrt(count)))))


def _moving_block_bootstrap_means(
    clean: List[float],
    *,
    samples: int,
    block_size: int,
    seed: int,
) -> List[float]:
    count = len(clean)
    if count <= 0:
        return []
    width = _resolved_block_size(count, block_size)
    starts = list(range(max(1, count - width + 1)))
    rng = random.Random(int(seed))
    estimates = []
    for _ in range(max(200, int(samples or 1000))):
        draw = []
        while len(draw) < count:
            start = starts[rng.randrange(len(starts))]
            draw.extend(clean[start : start + width])
        estimates.append(sum(draw[:count]) / count)
    return estimates


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

