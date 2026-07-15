"""离线回测校准脚本（B1）。

用滚动 AlphaLite 回测的 win_rate + avg_period_return 作为目标，
对 backtest._alphalite_signal 的权重做坐标下降式扫描，把更优权重写入
.runtime/weights.json 的 "alphalite_signal" 段。scoring/backtest 启动时若发现
该文件则自动加载覆盖（A7）。

纯离线、确定性：默认只读历史缓存（history_cache.sqlite3），不依赖实时行情；
缓存为空时可用 --codes 触发一次性抓取。

用法：
    python -m stock_analyzer.calibrate                 # 用缓存里全部代码
    python -m stock_analyzer.calibrate --codes 600000,000001,300750
    python -m stock_analyzer.calibrate --dry-run       # 只评估默认权重，不写文件
"""

import argparse
import copy
import itertools
import json
import math
import os
import sqlite3
import statistics
from contextlib import closing
from datetime import datetime
from typing import Dict, List

import pandas as pd

from . import config
from .backtest import _DEFAULT_ALPHALITE_WEIGHTS, run_rolling_alphalite_backtest
from .daily_data import list_market_data_codes, load_history_frames
from .history_cache import HistoryCache
from .normalization import coerce_number, normalize_code
from .runtime_json import atomic_write_json
from .scoring_core.scoring_math import _combine_details
from .scoring_core.weights import STRATEGY_COMBINERS, WEIGHTS
from .strategy_validation import StrategyValidationStore
from .expected_return_model import predict_expected_return
from .meta_labeling import predict_meta_confidence, train_meta_label_model
from .probability_calibration import build_and_save_calibrator, train_score_calibrator
from .validation_statistics import (
    benjamini_hochberg_fdr as _benjamini_hochberg_fdr,
    deflated_sharpe_ratio,
    moving_block_bootstrap_positive_mean_p_value,
    paired_increment_statistics,
    unified_experiment_fdr,
)
from .experiment_registry import list_experiments


def _cached_codes(db_path: str) -> List[str]:
    market_codes = _market_data_codes(getattr(config, "MARKET_DATA_DB_PATH", ""))
    if market_codes:
        return market_codes
    if not os.path.exists(db_path):
        return []
    with closing(sqlite3.connect(db_path)) as conn:
        try:
            rows = conn.execute("SELECT DISTINCT code FROM daily_history").fetchall()
        except sqlite3.OperationalError:
            return []
    return [str(row[0]) for row in rows if row and row[0]]


def _market_data_codes(db_path: str) -> List[str]:
    return list_market_data_codes(db_path)


def _load_history(codes: List[str], fetch: bool) -> Dict[str, pd.DataFrame]:
    cache = HistoryCache(config.HISTORY_CACHE_PATH, config.HISTORY_CACHE_FRESHNESS_HOURS)
    history_by_code: Dict[str, pd.DataFrame] = load_history_frames(
        getattr(config, "MARKET_DATA_DB_PATH", ""),
        codes,
        days=200,
    )
    provider = None
    if fetch:
        # 仅在显式要求时才创建 provider 抓取，避免离线运行触网。
        from .providers import MarketDataProvider

        provider = MarketDataProvider()
    for code in codes:
        code = normalize_code(code)
        if code in history_by_code:
            continue
        history = cache.get(code, days=200)
        if (history is None or history.empty) and provider is not None:
            try:
                history = provider.get_history(code, days=200)
                if history is not None and not history.empty:
                    cache.set(code, history)
            except Exception:
                history = None
        if history is not None and not history.empty:
            history_by_code[code] = history
    return history_by_code


def _objective(
    metrics: Dict[str, object],
    strategy: str = "",
    direction_focused: bool = False,
) -> float:
    """目标函数：命中率为主，平均收益为辅。无有效交易则极低分。

    明日预测需要按主周期净收益评价（避免用信号口径的相对超额误导），
    其他策略维持原有的「平均周期收益」口径。
    """
    if not metrics:
        return -1e9
    if strategy == "tomorrow_picks":
        win_rate = float(metrics.get("absolute_win_rate", 0.0) or 0.0)
        avg_return = float(metrics.get("absolute_avg_period_return", 0.0) or 0.0)
        median_return = float(metrics.get("absolute_median_period_return", 0.0) or 0.0)
        loss_quantile = float(metrics.get("absolute_loss_quantile_return", 0.0) or 0.0)
        avg_drawdown = float(metrics.get("absolute_avg_max_drawdown", 0.0) or 0.0)
        max_drawdown = float(metrics.get("absolute_max_drawdown", avg_drawdown) or 0.0)
        avg_open_gap = float(metrics.get("absolute_avg_next_open_return", 0.0) or 0.0)
        return_series = metrics.get("return_series") or []
        sortino = _sortino_ratio(return_series, avg_return) if return_series else 0.0
        tail_risk = min(0.0, loss_quantile) * 1.6 + min(0.0, avg_drawdown) * 0.25 + min(0.0, max_drawdown) * 0.5
        if not direction_focused:
            base = win_rate + avg_return * 2.0 + min(0.0, loss_quantile) * 1.6 + min(0.0, avg_drawdown) * 0.25
            if bool(getattr(config, "CALIBRATE_USE_SORTINO", True)) and return_series:
                base += sortino * 0.4 + min(0.0, max_drawdown) * 0.5
            return round(base * _time_decay_multiplier(metrics), 6)
        direction_weight = 3.0 if direction_focused else 2.0
        return_weight = 0.6 if direction_focused else 2.0
        median_weight = 0.2 if direction_focused else 1.2
        open_gap_weight = 0.2 if direction_focused else 0.5
        base = (
            win_rate * direction_weight
            + avg_return * return_weight
            + median_return * median_weight
            + avg_open_gap * open_gap_weight
            + tail_risk
        )
        if bool(getattr(config, "CALIBRATE_USE_SORTINO", True)) and return_series:
            base += sortino * 0.4
        return round(base * _time_decay_multiplier(metrics), 6)
    else:
        win_rate = float(metrics.get("win_rate", 0.0) or 0.0)
        avg_return = float(metrics.get("avg_period_return", 0.0) or 0.0)
    base = win_rate + avg_return * 2.0
    return_series = metrics.get("return_series") or []
    if bool(getattr(config, "CALIBRATE_USE_SORTINO", True)) and return_series:
        base += _sortino_ratio(return_series, avg_return) * 0.5
    return round(base * _time_decay_multiplier(metrics), 6)


def _sortino_ratio(return_series: List[float], avg_return: float = 0.0) -> float:
    returns = [coerce_number(value) for value in (return_series or [])]
    negative_returns = [value for value in returns if value < 0]
    if len(negative_returns) >= 2:
        downside_std = statistics.stdev(negative_returns)
    else:
        downside_std = abs(coerce_number(avg_return)) * 2.0 if coerce_number(avg_return) < 0 else 2.0
    return round(coerce_number(avg_return) / downside_std, 6) if downside_std > 1e-8 else 0.0


def _time_decay_multiplier(metrics: Dict[str, object]) -> float:
    if not bool(getattr(config, "CALIBRATE_USE_TIME_DECAY", True)):
        return 1.0
    paired = metrics.get("return_series_with_dates") or []
    if not paired:
        return 1.0
    overall = _time_weighted_win_rate(paired, int(getattr(config, "CALIBRATE_TIME_DECAY_HALF_LIFE", 60)))
    recent = _time_weighted_win_rate(paired, max(10, int(getattr(config, "CALIBRATE_TIME_DECAY_HALF_LIFE", 60)) // 2))
    if overall is None or recent is None:
        return 1.0
    drift = max(-20.0, min(20.0, recent - overall))
    return round(max(0.75, min(1.25, 1.0 + drift / 100.0)), 6)


def _time_weighted_win_rate(returns_with_dates: List[object], half_life: int = 60):
    parsed = []
    for item in returns_with_dates or []:
        try:
            date_value, return_value = item
        except Exception:
            continue
        parsed_date = _parse_sample_date(date_value)
        if parsed_date is None:
            continue
        parsed.append((parsed_date, coerce_number(return_value)))
    if not parsed:
        return None
    latest = max(date for date, _ in parsed)
    half_life_days = max(1, int(half_life or 60))
    weighted_total = 0.0
    weighted_wins = 0.0
    for date_value, return_value in parsed:
        age_days = max(0, (latest - date_value).days)
        weight = 0.5 ** (age_days / half_life_days)
        weighted_total += weight
        weighted_wins += weight if return_value > 0 else 0.0
    return weighted_wins / weighted_total * 100.0 if weighted_total > 0 else None


def _parse_sample_date(value):
    text = str(value or "").strip()
    if not text:
        return None
    compact = text[:10].replace("-", "")
    for fmt, raw in (("%Y%m%d", compact), ("%Y-%m-%d", text[:10])):
        try:
            return datetime.strptime(raw, fmt).date()
        except Exception:
            continue
    return None


def _resolve_tomorrow_direction_focus(direction_focus) -> bool:
    if direction_focus is None:
        return bool(getattr(config, "CALIBRATE_TOMORROW_DIRECTION_FOCUSED", False))
    return bool(direction_focus)


def _tomorrow_direction_objective(metrics: Dict[str, object]) -> float:
    if not metrics:
        return -1e9
    return _objective(metrics, strategy="tomorrow_picks", direction_focused=True)


def _evaluate(history_by_code, weights, top_k, holding_days, lookback_days) -> Dict[str, object]:
    result = run_rolling_alphalite_backtest(
        history_by_code,
        top_k=top_k,
        holding_days=holding_days,
        lookback_days=lookback_days,
        weights=weights,
    )
    return result.get("metrics", {}) if result.get("ok") else {}


def calibrate(
    history_by_code: Dict[str, pd.DataFrame],
    top_k: int = 10,
    holding_days: int = 3,
    lookback_days: int = 30,
    steps: int = 2,
) -> Dict[str, object]:
    """对每个权重在 {0.7x, 1.0x, 1.3x} 三档上做坐标下降，返回最优权重与对比指标。"""
    best = copy.deepcopy(_DEFAULT_ALPHALITE_WEIGHTS)
    baseline_metrics = _evaluate(history_by_code, best, top_k, holding_days, lookback_days)
    best_obj = _objective(baseline_metrics)
    best_metrics = baseline_metrics

    multipliers = (0.7, 1.0, 1.3)
    for _ in range(max(1, steps)):
        improved = False
        for key in best:
            for mult in multipliers:
                if mult == 1.0:
                    continue
                candidate = copy.deepcopy(best)
                candidate[key] = round(best[key] * mult, 4)
                metrics = _evaluate(history_by_code, candidate, top_k, holding_days, lookback_days)
                obj = _objective(metrics)
                if obj > best_obj + 1e-9:
                    best, best_obj, best_metrics, improved = candidate, obj, metrics, True
        if not improved:
            break

    return {
        "weights": best,
        "baseline_metrics": baseline_metrics,
        "best_metrics": best_metrics,
        "baseline_objective": round(_objective(baseline_metrics), 4),
        "best_objective": round(best_obj, 4),
    }


def compare_momentum(
    history_by_code: Dict[str, pd.DataFrame],
    top_k: int = 10,
    holding_days: int = 3,
    lookback_days: int = 30,
) -> Dict[str, object]:
    """对比"动量倾向"vs"反转倾向"两套 AlphaLite 信号在滚动回测上的表现。

    A 股短线证据显示反转优于动量。本函数用回测数据验证：把近期收益权重取正
    （动量）vs 取负（反转），看哪套 win_rate+收益更好，据此建议 today_term 的
    reversal_tilt 取值（0 = 维持动量；>0 = 启用反转修正）。
    """
    momentum_w = copy.deepcopy(_DEFAULT_ALPHALITE_WEIGHTS)  # 默认即动量倾向（ret 正权重）
    reversal_w = copy.deepcopy(_DEFAULT_ALPHALITE_WEIGHTS)
    for key in ("ret_5d", "ret_10d", "ret_20d"):
        reversal_w[key] = -abs(reversal_w[key])  # 反转：近期涨得多→分低

    mom_metrics = _evaluate(history_by_code, momentum_w, top_k, holding_days, lookback_days)
    rev_metrics = _evaluate(history_by_code, reversal_w, top_k, holding_days, lookback_days)
    mom_obj = _objective(mom_metrics)
    rev_obj = _objective(rev_metrics)
    reversal_wins = rev_obj > mom_obj + 1e-9
    # 反转更优时，按优势幅度给一个温和的 reversal_tilt 建议（封顶 0.6）。
    suggested_tilt = 0.0
    if reversal_wins:
        edge = rev_obj - mom_obj
        suggested_tilt = round(min(0.6, 0.2 + edge / 100.0), 3)

    return {
        "momentum_metrics": mom_metrics,
        "reversal_metrics": rev_metrics,
        "momentum_objective": round(mom_obj, 4),
        "reversal_objective": round(rev_obj, 4),
        "reversal_wins": reversal_wins,
        "suggested_reversal_tilt": suggested_tilt,
    }


def calibrate_live_weights(
    strategy: str,
    db_path: str = "",
    top_k: int = 10,
    days: int = 120,
    steps: int = 2,
    dry_run: bool = True,
    direction_focus=None,
) -> Dict[str, object]:
    if strategy != "tomorrow_picks":
        return {"ok": False, "strategy": strategy, "status": "unsupported_strategy", "supported_strategy": "tomorrow_picks"}
    if strategy not in STRATEGY_COMBINERS:
        return {"ok": False, "strategy": strategy, "status": "unknown_strategy"}
    store = StrategyValidationStore(db_path or config.VALIDATION_DB_PATH)
    samples = store.live_weight_samples(strategy, days=days)
    samples = [sample for sample in samples if _sample_has_live_components(strategy, sample.get("raw") or {})]
    min_samples = int(getattr(config, "CALIBRATE_MIN_SAMPLES", 30))
    if len(samples) < min_samples:
        return {
            "ok": True,
            "strategy": strategy,
            "status": "insufficient_samples",
            "sample_count": len(samples),
            "min_samples": min_samples,
        }
    avg_coverage = _average_sample_coverage(samples)
    min_coverage = coerce_number(getattr(config, "CALIBRATE_MIN_COVERAGE", 0.5), 0.5)
    if avg_coverage < min_coverage:
        return {
            "ok": True,
            "strategy": strategy,
            "status": "insufficient_factor_coverage",
            "sample_count": len(samples),
            "avg_data_coverage": round(avg_coverage, 4),
            "min_data_coverage": min_coverage,
        }

    current = _current_strategy_weights(strategy)
    direction_focus = _resolve_tomorrow_direction_focus(direction_focus)
    baseline_metrics = _evaluate_live_samples(strategy, samples, current, top_k=top_k)
    baseline_obj = _objective(baseline_metrics, strategy, direction_focused=direction_focus)
    fitted = _fit_weights(strategy, samples, top_k=top_k, steps=steps, initial=current, direction_focus=direction_focus)
    best = fitted["weights"]
    best_metrics = fitted["metrics"]
    best_obj = fitted["objective"]
    walk_forward = _walk_forward_evaluate(
        strategy,
        samples,
        current,
        top_k=top_k,
        steps=steps,
        direction_focus=direction_focus,
    )

    margin = float(getattr(config, "CALIBRATE_IMPROVE_MARGIN", 0.05))
    keys = _strategy_weight_keys(strategy)
    patch = {key: best[key] for key in keys}
    result = {
        "ok": True,
        "strategy": strategy,
        "status": "dry_run" if dry_run else "no_improvement",
        "sample_count": len(samples),
        "top_k": top_k,
        "baseline_metrics": baseline_metrics,
        "best_metrics": best_metrics,
        "baseline_objective": round(baseline_obj, 4),
        "best_objective": round(best_obj, 4),
        "objective_mode": "direction_focused" if direction_focus else "default",
        "baseline_direction_objective": round(_tomorrow_direction_objective(baseline_metrics), 4),
        "best_direction_objective": round(_tomorrow_direction_objective(best_metrics), 4),
        "improvement": round(best_obj - baseline_obj, 4),
        "oos_baseline_objective": walk_forward.get("baseline_oos_objective"),
        "oos_best_objective": walk_forward.get("best_oos_objective"),
        "oos_improvement": walk_forward.get("oos_improvement"),
        "oos_positive_folds": walk_forward.get("positive_folds", 0),
        "oos_fold_count": walk_forward.get("fold_count", 0),
        "folds": walk_forward.get("folds", []),
        "weights": patch,
    }
    score_calibrator = train_score_calibrator(strategy, samples)
    result["score_calibration"] = {
        "status": "ready" if score_calibrator.is_fitted else "insufficient_samples",
        "sample_count": score_calibrator.sample_count,
        "bucket_count": len(score_calibrator.buckets),
    }
    if not walk_forward.get("ok"):
        result["status"] = walk_forward.get("status", "insufficient_oos_folds")
        result["margin"] = margin
        return result
    oos_baseline = coerce_number(walk_forward.get("baseline_oos_objective"), -1e9)
    oos_best = coerce_number(walk_forward.get("best_oos_objective"), -1e9)
    positive_folds = int(walk_forward.get("positive_folds") or 0)
    fold_count = int(walk_forward.get("fold_count") or 0)
    if oos_best <= oos_baseline + margin or positive_folds <= fold_count // 2:
        result["status"] = "no_oos_improvement"
        result["margin"] = margin
        return result
    if dry_run:
        result["status"] = "dry_run_improved"
        result["margin"] = margin
        return result
    _write_weights_override({"weights": {strategy: patch}})
    result["score_calibration"] = build_and_save_calibrator(strategy, samples)
    result["status"] = "written"
    result["path"] = config.WEIGHTS_OVERRIDE_PATH
    return result



def evaluate_expected_return_ranker(
    strategy: str,
    samples: List[Dict[str, object]],
    top_k: int = 10,
) -> Dict[str, object]:
    if strategy not in STRATEGY_COMBINERS:
        return {"ok": False, "strategy": strategy, "status": "unknown_strategy"}
    samples = [sample for sample in samples if isinstance(sample, dict)]
    folds = _walk_forward_splits(
        samples,
        int(getattr(config, "CALIBRATE_WALK_FORWARD_FOLDS", 4)),
        purge_days=_strategy_oos_purge_days(strategy),
    )
    if not folds:
        return {"ok": False, "strategy": strategy, "status": "insufficient_oos_folds", "folds": [], "fold_count": 0}
    rows = []
    for index, (train_samples, test_samples, train_dates, test_dates) in enumerate(folds, start=1):
        baseline_metrics = _metrics_from_score_rank(test_samples, "score", top_k=top_k)
        rank_metrics = _metrics_from_expected_return_rank(strategy, train_samples, test_samples, top_k=top_k)
        baseline_obj = _objective(baseline_metrics, strategy, direction_focused=_resolve_tomorrow_direction_focus(None))
        rank_obj = _objective(rank_metrics, strategy, direction_focused=_resolve_tomorrow_direction_focus(None))
        rows.append(
            {
                "fold": index,
                "train_start": train_dates[0],
                "train_end": train_dates[-1],
                "test_start": test_dates[0],
                "test_end": test_dates[-1],
                "train_sample_count": len(train_samples),
                "test_sample_count": len(test_samples),
                "baseline_oos_objective": round(baseline_obj, 4),
                "predicted_net_return_oos_objective": round(rank_obj, 4),
                "rank_score_oos_objective": round(rank_obj, 4),
                "oos_improvement": round(rank_obj - baseline_obj, 4),
                "baseline_avg_return": baseline_metrics.get("absolute_avg_period_return", 0.0),
                "predicted_net_return_avg_return": rank_metrics.get("absolute_avg_period_return", 0.0),
                "rank_score_avg_return": rank_metrics.get("absolute_avg_period_return", 0.0),
                "baseline_return_series_with_dates": baseline_metrics.get("return_series_with_dates", []),
                "predicted_return_series_with_dates": rank_metrics.get("return_series_with_dates", []),
            }
        )
    baseline_values = [coerce_number(row["baseline_oos_objective"]) for row in rows]
    rank_values = [coerce_number(row["predicted_net_return_oos_objective"]) for row in rows]
    improvements = [coerce_number(row["oos_improvement"]) for row in rows]
    avg_return_improvements = [
        coerce_number(row.get("predicted_net_return_avg_return")) - coerce_number(row.get("baseline_avg_return"))
        for row in rows
    ]
    margin = float(getattr(config, "CALIBRATE_IMPROVE_MARGIN", 0.05))
    baseline_oos = round(sum(baseline_values) / len(baseline_values), 4)
    rank_oos = round(sum(rank_values) / len(rank_values), 4)
    positive_folds = sum(1 for value in improvements if value > 0)
    fold_count = len(rows)
    daily_increments = _daily_increment_series(
        rows, "baseline_return_series_with_dates", "predicted_return_series_with_dates"
    )
    fdr_result = _calibrate_fdr_result(positive_folds, fold_count, increments=daily_increments)
    dsr_result = fdr_result.get("dsr") or {}
    ci_low, ci_high = _mean_ci(avg_return_improvements)
    ci_result = {
        "method": "normal_approx_fold_avg_return_delta",
        "confidence": 0.95,
        "metric": "predicted_net_return_avg_return_minus_baseline",
        "sample_count": len(avg_return_improvements),
        "low": ci_low,
        "high": ci_high,
        "passed": ci_low is not None and ci_low >= 0.0,
    }
    oos_passed = rank_oos > baseline_oos + margin and positive_folds > fold_count // 2
    can_promote = (
        oos_passed
        and bool(fdr_result.get("passed"))
        and bool(ci_result.get("passed"))
        and bool(dsr_result.get("passed"))
    )
    status = "oos_passed" if can_promote else "shadow_only"
    if oos_passed and not fdr_result.get("passed"):
        status = "fdr_blocked"
    elif oos_passed and fdr_result.get("passed") and not ci_result.get("passed"):
        status = "ci_blocked"
    elif oos_passed and fdr_result.get("passed") and ci_result.get("passed") and not dsr_result.get("passed"):
        status = "dsr_blocked"
    return {
        "ok": True,
        "strategy": strategy,
        "status": status,
        "sample_count": len(samples),
        "top_k": top_k,
        "baseline_oos_objective": baseline_oos,
        "predicted_net_return_oos_objective": rank_oos,
        "rank_score_oos_objective": rank_oos,
        "oos_improvement": round(rank_oos - baseline_oos, 4),
        "positive_folds": positive_folds,
        "fold_count": fold_count,
        "oos_passed": oos_passed,
        "fdr": fdr_result,
        "dsr": dsr_result,
        "ci": ci_result,
        "avg_return_improvement": round(sum(avg_return_improvements) / len(avg_return_improvements), 4),
        "avg_return_improvement_ci95_low": ci_low,
        "avg_return_improvement_ci95_high": ci_high,
        "margin": margin,
        "can_promote": can_promote,
        "folds": rows,
        "daily_increment_series": daily_increments,
    }


def evaluate_interaction_ranker(
    strategy: str,
    samples: List[Dict[str, object]],
    top_k: int = 10,
    max_pairs: int = None,
) -> Dict[str, object]:
    """Walk-forward shadow evaluation for second-order factor interactions."""
    if strategy not in STRATEGY_COMBINERS:
        return {"ok": False, "strategy": strategy, "status": "unknown_strategy"}
    samples = [sample for sample in samples if isinstance(sample, dict)]
    samples = [
        sample
        for sample in samples
        if _sample_has_live_components(strategy, sample.get("raw") or {})
        and sample.get("signal_date")
        and sample.get("primary_return_net") is not None
    ]
    folds = _walk_forward_splits(
        samples,
        int(getattr(config, "CALIBRATE_WALK_FORWARD_FOLDS", 4)),
        purge_days=_strategy_oos_purge_days(strategy),
    )
    if not folds:
        return {"ok": False, "strategy": strategy, "status": "insufficient_oos_folds", "folds": [], "fold_count": 0}

    current = _current_strategy_weights(strategy)
    max_pairs = int(max_pairs or getattr(config, "INTERACTION_TERM_MAX_PAIRS", 8))
    direction_focus = _resolve_tomorrow_direction_focus(None)
    rows = []
    for index, (train_samples, test_samples, train_dates, test_dates) in enumerate(folds, start=1):
        model = _fit_interaction_terms(strategy, train_samples, max_pairs=max_pairs)
        baseline_metrics = _evaluate_live_samples(strategy, test_samples, current, top_k=top_k)
        interaction_metrics = _evaluate_interaction_samples(strategy, test_samples, current, model, top_k=top_k)
        baseline_obj = _objective(baseline_metrics, strategy, direction_focused=direction_focus)
        interaction_obj = _objective(interaction_metrics, strategy, direction_focused=direction_focus)
        rows.append(
            {
                "fold": index,
                "train_start": train_dates[0],
                "train_end": train_dates[-1],
                "test_start": test_dates[0],
                "test_end": test_dates[-1],
                "train_sample_count": len(train_samples),
                "test_sample_count": len(test_samples),
                "selected_interactions": model.get("interactions", []),
                "baseline_oos_objective": round(baseline_obj, 4),
                "interaction_oos_objective": round(interaction_obj, 4),
                "oos_improvement": round(interaction_obj - baseline_obj, 4),
                "baseline_avg_return": baseline_metrics.get("absolute_avg_period_return", 0.0),
                "interaction_avg_return": interaction_metrics.get("absolute_avg_period_return", 0.0),
                "baseline_return_series_with_dates": baseline_metrics.get("return_series_with_dates", []),
                "interaction_return_series_with_dates": interaction_metrics.get("return_series_with_dates", []),
            }
        )

    baseline_values = [coerce_number(row["baseline_oos_objective"]) for row in rows]
    interaction_values = [coerce_number(row["interaction_oos_objective"]) for row in rows]
    improvements = [coerce_number(row["oos_improvement"]) for row in rows]
    margin = float(getattr(config, "CALIBRATE_IMPROVE_MARGIN", 0.05))
    baseline_oos = round(sum(baseline_values) / len(baseline_values), 4)
    interaction_oos = round(sum(interaction_values) / len(interaction_values), 4)
    positive_folds = sum(1 for value in improvements if value > 0)
    fold_count = len(rows)
    daily_increments = _daily_increment_series(
        rows, "baseline_return_series_with_dates", "interaction_return_series_with_dates"
    )
    fdr_result = _calibrate_fdr_result(positive_folds, fold_count, increments=daily_increments)
    dsr_result = fdr_result.get("dsr") or {}
    oos_passed = interaction_oos > baseline_oos + margin and positive_folds > fold_count // 2
    can_promote = oos_passed and bool(fdr_result.get("passed")) and bool(dsr_result.get("passed"))
    status = "oos_passed" if can_promote else "shadow_only"
    if oos_passed and not fdr_result.get("passed"):
        status = "fdr_blocked"
    elif oos_passed and fdr_result.get("passed") and not dsr_result.get("passed"):
        status = "dsr_blocked"
    final_model = _fit_interaction_terms(strategy, samples, max_pairs=max_pairs)
    return {
        "ok": True,
        "strategy": strategy,
        "status": status,
        "sample_count": len(samples),
        "top_k": top_k,
        "enabled": bool(getattr(config, "ENABLE_INTERACTION_TERMS", False)),
        "baseline_oos_objective": baseline_oos,
        "interaction_oos_objective": interaction_oos,
        "oos_improvement": round(interaction_oos - baseline_oos, 4),
        "positive_folds": positive_folds,
        "fold_count": fold_count,
        "margin": margin,
        "fdr": fdr_result,
        "dsr": dsr_result,
        "can_promote": can_promote,
        "selected_interactions": final_model.get("interactions", []),
        "folds": rows,
        "daily_increment_series": daily_increments,
    }


def evaluate_regime_specific_weights(
    strategy: str,
    samples: List[Dict[str, object]],
    top_k: int = 10,
    steps: int = 2,
) -> Dict[str, object]:
    """Walk-forward shadow evaluation for regime-specific strategy weights."""
    if strategy not in STRATEGY_COMBINERS:
        return {"ok": False, "strategy": strategy, "status": "unknown_strategy"}
    samples = [sample for sample in samples if isinstance(sample, dict)]
    samples = [
        sample
        for sample in samples
        if _sample_has_live_components(strategy, sample.get("raw") or {})
        and sample.get("signal_date")
        and sample.get("primary_return_net") is not None
    ]
    folds = _walk_forward_splits(
        samples,
        int(getattr(config, "CALIBRATE_WALK_FORWARD_FOLDS", 4)),
        purge_days=_strategy_oos_purge_days(strategy),
    )
    if not folds:
        return {"ok": False, "strategy": strategy, "status": "insufficient_oos_folds", "folds": [], "fold_count": 0}

    current = _current_strategy_weights(strategy)
    direction_focus = _resolve_tomorrow_direction_focus(None)
    rows = []
    for index, (train_samples, test_samples, train_dates, test_dates) in enumerate(folds, start=1):
        regime_model = _fit_regime_specific_weights(
            strategy,
            train_samples,
            top_k=top_k,
            steps=steps,
            initial=current,
            direction_focus=direction_focus,
        )
        baseline_metrics = _evaluate_live_samples(strategy, test_samples, current, top_k=top_k)
        regime_metrics = _evaluate_regime_specific_samples(strategy, test_samples, current, regime_model, top_k=top_k)
        baseline_obj = _objective(baseline_metrics, strategy, direction_focused=direction_focus)
        regime_obj = _objective(regime_metrics, strategy, direction_focused=direction_focus)
        rows.append(
            {
                "fold": index,
                "train_start": train_dates[0],
                "train_end": train_dates[-1],
                "test_start": test_dates[0],
                "test_end": test_dates[-1],
                "train_sample_count": len(train_samples),
                "test_sample_count": len(test_samples),
                "regime_sample_counts": regime_model.get("sample_counts", {}),
                "fitted_regimes": sorted((regime_model.get("weights") or {}).keys()),
                "fallback_regimes": sorted(regime_model.get("fallback_regimes", [])),
                "baseline_oos_objective": round(baseline_obj, 4),
                "regime_oos_objective": round(regime_obj, 4),
                "oos_improvement": round(regime_obj - baseline_obj, 4),
                "baseline_avg_return": baseline_metrics.get("absolute_avg_period_return", 0.0),
                "regime_avg_return": regime_metrics.get("absolute_avg_period_return", 0.0),
                "baseline_return_series_with_dates": baseline_metrics.get("return_series_with_dates", []),
                "regime_return_series_with_dates": regime_metrics.get("return_series_with_dates", []),
            }
        )

    baseline_values = [coerce_number(row["baseline_oos_objective"]) for row in rows]
    regime_values = [coerce_number(row["regime_oos_objective"]) for row in rows]
    improvements = [coerce_number(row["oos_improvement"]) for row in rows]
    margin = float(getattr(config, "CALIBRATE_IMPROVE_MARGIN", 0.05))
    baseline_oos = round(sum(baseline_values) / len(baseline_values), 4)
    regime_oos = round(sum(regime_values) / len(regime_values), 4)
    positive_folds = sum(1 for value in improvements if value > 0)
    fold_count = len(rows)
    daily_increments = _daily_increment_series(
        rows, "baseline_return_series_with_dates", "regime_return_series_with_dates"
    )
    fdr_result = _calibrate_fdr_result(positive_folds, fold_count, increments=daily_increments)
    dsr_result = fdr_result.get("dsr") or {}
    oos_passed = regime_oos > baseline_oos + margin and positive_folds > fold_count // 2
    can_promote = oos_passed and bool(fdr_result.get("passed")) and bool(dsr_result.get("passed"))
    status = "oos_passed" if can_promote else "shadow_only"
    if oos_passed and not fdr_result.get("passed"):
        status = "fdr_blocked"
    elif oos_passed and fdr_result.get("passed") and not dsr_result.get("passed"):
        status = "dsr_blocked"
    final_model = _fit_regime_specific_weights(
        strategy,
        samples,
        top_k=top_k,
        steps=steps,
        initial=current,
        direction_focus=direction_focus,
    )
    return {
        "ok": True,
        "strategy": strategy,
        "status": status,
        "sample_count": len(samples),
        "top_k": top_k,
        "enabled": bool(getattr(config, "ENABLE_REGIME_SPECIFIC_WEIGHTS", False)),
        "baseline_oos_objective": baseline_oos,
        "regime_oos_objective": regime_oos,
        "oos_improvement": round(regime_oos - baseline_oos, 4),
        "positive_folds": positive_folds,
        "fold_count": fold_count,
        "margin": margin,
        "fdr": fdr_result,
        "dsr": dsr_result,
        "can_promote": can_promote,
        "weights_by_regime": final_model.get("weights", {}),
        "regime_sample_counts": final_model.get("sample_counts", {}),
        "fallback_regimes": sorted(final_model.get("fallback_regimes", [])),
        "folds": rows,
        "daily_increment_series": daily_increments,
    }


def evaluate_meta_labeling_gate(
    strategy: str,
    samples: List[Dict[str, object]],
    top_k: int = 10,
) -> Dict[str, object]:
    """Walk-forward shadow evaluation for meta-label confidence gating."""
    if strategy not in STRATEGY_COMBINERS:
        return {"ok": False, "strategy": strategy, "status": "unknown_strategy"}
    samples = [sample for sample in samples if isinstance(sample, dict)]
    samples = [
        sample
        for sample in samples
        if sample.get("signal_date") and sample.get("primary_return_net") is not None
    ]
    folds = _walk_forward_splits(
        samples,
        int(getattr(config, "CALIBRATE_WALK_FORWARD_FOLDS", 4)),
        purge_days=_strategy_oos_purge_days(strategy),
    )
    if not folds:
        return {"ok": False, "strategy": strategy, "status": "insufficient_oos_folds", "folds": [], "fold_count": 0}
    direction_focus = _resolve_tomorrow_direction_focus(None)
    rows = []
    for index, (train_samples, test_samples, train_dates, test_dates) in enumerate(folds, start=1):
        baseline_metrics = _metrics_from_score_rank(test_samples, "score", top_k=top_k)
        meta_metrics, model_info = _metrics_from_meta_label_rank(strategy, train_samples, test_samples, top_k=top_k)
        baseline_obj = _objective(baseline_metrics, strategy, direction_focused=direction_focus)
        meta_obj = _objective(meta_metrics, strategy, direction_focused=direction_focus)
        rows.append(
            {
                "fold": index,
                "train_start": train_dates[0],
                "train_end": train_dates[-1],
                "test_start": test_dates[0],
                "test_end": test_dates[-1],
                "train_sample_count": len(train_samples),
                "test_sample_count": len(test_samples),
                "model_status": model_info.get("status"),
                "model_sample_count": model_info.get("sample_count", 0),
                "baseline_oos_objective": round(baseline_obj, 4),
                "meta_oos_objective": round(meta_obj, 4),
                "oos_improvement": round(meta_obj - baseline_obj, 4),
                "baseline_avg_return": baseline_metrics.get("absolute_avg_period_return", 0.0),
                "meta_avg_return": meta_metrics.get("absolute_avg_period_return", 0.0),
                "baseline_return_series_with_dates": baseline_metrics.get("return_series_with_dates", []),
                "meta_return_series_with_dates": meta_metrics.get("return_series_with_dates", []),
            }
        )
    baseline_values = [coerce_number(row["baseline_oos_objective"]) for row in rows]
    meta_values = [coerce_number(row["meta_oos_objective"]) for row in rows]
    improvements = [coerce_number(row["oos_improvement"]) for row in rows]
    margin = float(getattr(config, "CALIBRATE_IMPROVE_MARGIN", 0.05))
    baseline_oos = round(sum(baseline_values) / len(baseline_values), 4)
    meta_oos = round(sum(meta_values) / len(meta_values), 4)
    positive_folds = sum(1 for value in improvements if value > 0)
    fold_count = len(rows)
    daily_increments = _daily_increment_series(
        rows, "baseline_return_series_with_dates", "meta_return_series_with_dates"
    )
    fdr_result = _calibrate_fdr_result(positive_folds, fold_count, increments=daily_increments)
    dsr_result = fdr_result.get("dsr") or {}
    oos_passed = meta_oos > baseline_oos + margin and positive_folds > fold_count // 2
    can_enforce = oos_passed and bool(fdr_result.get("passed")) and bool(dsr_result.get("passed"))
    status = "oos_passed" if can_enforce else "shadow_only"
    if oos_passed and not fdr_result.get("passed"):
        status = "fdr_blocked"
    elif oos_passed and fdr_result.get("passed") and not dsr_result.get("passed"):
        status = "dsr_blocked"
    final_model = train_meta_label_model(strategy, samples)
    return {
        "ok": True,
        "strategy": strategy,
        "status": status,
        "sample_count": len(samples),
        "top_k": top_k,
        "enabled": bool(getattr(config, "ENABLE_META_LABELING", False)),
        "baseline_oos_objective": baseline_oos,
        "meta_oos_objective": meta_oos,
        "oos_improvement": round(meta_oos - baseline_oos, 4),
        "positive_folds": positive_folds,
        "fold_count": fold_count,
        "margin": margin,
        "fdr": fdr_result,
        "dsr": dsr_result,
        "can_enforce": can_enforce,
        "model_status": final_model.get("status"),
        "model_sample_count": final_model.get("sample_count", 0),
        "folds": rows,
        "daily_increment_series": daily_increments,
    }



def _daily_increment_series(
    rows: List[Dict[str, object]],
    baseline_field: str,
    challenger_field: str,
) -> List[float]:
    """Collapse cross-sectional fold outputs to one paired observation per day."""
    baseline_by_date: Dict[str, List[float]] = {}
    challenger_by_date: Dict[str, List[float]] = {}
    for row in rows or []:
        for date_value, value in row.get(baseline_field) or []:
            baseline_by_date.setdefault(str(date_value), []).append(coerce_number(value))
        for date_value, value in row.get(challenger_field) or []:
            challenger_by_date.setdefault(str(date_value), []).append(coerce_number(value))
    increments = []
    for date_value in sorted(set(baseline_by_date) & set(challenger_by_date)):
        baseline = sum(baseline_by_date[date_value]) / len(baseline_by_date[date_value])
        challenger = sum(challenger_by_date[date_value]) / len(challenger_by_date[date_value])
        increments.append(round(challenger - baseline, 6))
    return increments


def _metrics_from_score_rank(samples: List[Dict[str, object]], score_key: str, top_k: int) -> Dict[str, object]:
    grouped: Dict[str, List[Dict[str, object]]] = {}
    for sample in samples:
        raw = sample.get("raw") or {}
        item = dict(sample)
        item["recomputed_score"] = coerce_number(raw.get(score_key), coerce_number(sample.get("stored_score")))
        grouped.setdefault(str(sample.get("signal_date")), []).append(item)
    return _metrics_from_ranked_groups(grouped, top_k)


def _metrics_from_expected_return_rank(
    strategy: str,
    train_samples: List[Dict[str, object]],
    test_samples: List[Dict[str, object]],
    top_k: int,
) -> Dict[str, object]:
    grouped: Dict[str, List[Dict[str, object]]] = {}
    rows = []
    for sample in test_samples:
        raw = sample.get("raw") if isinstance(sample, dict) else {}
        raw = raw if isinstance(raw, dict) else {}
        rows.append(
            {
                **raw,
                "sample": sample,
                "score": coerce_number(raw.get("score"), coerce_number(sample.get("stored_score"))),
                "risk_penalty": coerce_number(raw.get("risk_penalty")),
            }
        )
    ranked_rows = predict_expected_return(strategy, rows, samples=train_samples)
    usable_rows = [
        item
        for item in ranked_rows
        if coerce_number(item.get("predicted_net_return"), None) is not None
        and str(item.get("model_confidence") or "").strip().lower() in {"shadow", "ready"}
    ]
    if len(usable_rows) < len(ranked_rows):
        return {
            "absolute_avg_period_return": 0.0,
            "win_rate": 0.0,
            "selection_count": 0,
            "status": "insufficient_expected_return_predictions",
        }
    for item in usable_rows:
        sample = dict(item.get("sample") or {})
        sample["recomputed_score"] = coerce_number(item.get("predicted_net_return"))
        grouped.setdefault(str(sample.get("signal_date")), []).append(sample)
    return _metrics_from_ranked_groups(grouped, top_k)


def _metrics_from_meta_label_rank(
    strategy: str,
    train_samples: List[Dict[str, object]],
    test_samples: List[Dict[str, object]],
    top_k: int,
) -> tuple:
    model = train_meta_label_model(strategy, train_samples)
    if not model.get("is_fitted"):
        return _metrics_from_score_rank(test_samples, "score", top_k=top_k), model
    grouped: Dict[str, List[Dict[str, object]]] = {}
    for sample in test_samples:
        raw = sample.get("raw") if isinstance(sample, dict) else {}
        raw = raw if isinstance(raw, dict) else {}
        base_score = coerce_number(raw.get("score"), coerce_number(sample.get("stored_score")))
        row = {
            **raw,
            "score": base_score,
            "risk_penalty": coerce_number(raw.get("risk_penalty"), 0.0),
        }
        prediction = predict_meta_confidence(row, model)
        confidence = coerce_number(prediction.get("confidence"), 0.5)
        action = str(prediction.get("action") or "")
        item = dict(sample)
        if action == "skip":
            item["recomputed_score"] = base_score - 100.0
        elif action == "reduced":
            item["recomputed_score"] = base_score * (0.75 + confidence * 0.25)
        else:
            item["recomputed_score"] = base_score * (0.9 + confidence * 0.2)
        item["meta_labeling"] = prediction
        grouped.setdefault(str(sample.get("signal_date")), []).append(item)
    return _metrics_from_ranked_groups(grouped, top_k), model


def _evaluate_interaction_samples(
    strategy: str,
    samples: List[Dict[str, object]],
    weights: Dict[str, float],
    model: Dict[str, object],
    top_k: int,
) -> Dict[str, object]:
    grouped: Dict[str, List[Dict[str, object]]] = {}
    for sample in samples:
        raw = sample.get("raw") or {}
        components = _live_components(raw)
        combined = _combine_details(
            components,
            strategy,
            weights={**WEIGHTS, strategy: weights},
            row=pd.Series(raw),
            regime_weight_profile=raw.get("regime_weight_profile") or {},
        )
        enriched = dict(sample)
        score = combined["score"] + _interaction_score_delta(components, model)
        enriched["recomputed_score"] = max(0.0, min(100.0, score))
        grouped.setdefault(str(sample.get("signal_date")), []).append(enriched)
    return _metrics_from_ranked_groups(grouped, top_k)


def _evaluate_regime_specific_samples(
    strategy: str,
    samples: List[Dict[str, object]],
    default_weights: Dict[str, float],
    model: Dict[str, object],
    top_k: int,
) -> Dict[str, object]:
    grouped: Dict[str, List[Dict[str, object]]] = {}
    weights_by_regime = model.get("weights") if isinstance(model, dict) else {}
    weights_by_regime = weights_by_regime if isinstance(weights_by_regime, dict) else {}
    for sample in samples:
        raw = sample.get("raw") or {}
        regime = _sample_regime_level(sample)
        weights = weights_by_regime.get(regime, default_weights)
        components = _live_components(raw)
        combined = _combine_details(
            components,
            strategy,
            weights={**WEIGHTS, strategy: weights},
            row=pd.Series(raw),
            regime_weight_profile=raw.get("regime_weight_profile") or {},
        )
        enriched = dict(sample)
        enriched["recomputed_score"] = combined["score"]
        enriched["regime_level"] = regime
        grouped.setdefault(str(sample.get("signal_date")), []).append(enriched)
    return _metrics_from_ranked_groups(grouped, top_k)


def _fit_regime_specific_weights(
    strategy: str,
    samples: List[Dict[str, object]],
    top_k: int,
    steps: int,
    initial: Dict[str, float],
    direction_focus=None,
) -> Dict[str, object]:
    min_samples = int(getattr(config, "REGIME_SPECIFIC_MIN_TRAIN_SAMPLES", 20))
    grouped: Dict[str, List[Dict[str, object]]] = {"risk_on": [], "balanced": [], "risk_off": []}
    unknown = []
    for sample in samples:
        regime = _sample_regime_level(sample)
        if regime in grouped:
            grouped[regime].append(sample)
        else:
            unknown.append(sample)
    weights = {}
    fallback_regimes = []
    sample_counts = {key: len(value) for key, value in grouped.items()}
    for regime, regime_samples in grouped.items():
        if len(regime_samples) < min_samples:
            fallback_regimes.append(regime)
            continue
        fitted = _fit_weights(
            strategy,
            regime_samples,
            top_k=top_k,
            steps=steps,
            initial=initial,
            direction_focus=direction_focus,
        )
        weights[regime] = fitted["weights"]
    return {
        "strategy": strategy,
        "status": "ready" if weights else "insufficient_regime_samples",
        "min_samples": min_samples,
        "sample_counts": sample_counts,
        "unknown_sample_count": len(unknown),
        "weights": weights,
        "fallback_regimes": fallback_regimes,
    }


def _sample_regime_level(sample: Dict[str, object]) -> str:
    raw = sample.get("raw") if isinstance(sample, dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    candidates = [
        raw.get("regime_level"),
        raw.get("market_regime_level"),
        raw.get("market_regime"),
        sample.get("regime_level"),
    ]
    nested = raw.get("market_regime") if isinstance(raw.get("market_regime"), dict) else None
    if nested:
        candidates.insert(0, nested.get("level"))
    for value in candidates:
        level = _normalize_regime_level(value)
        if level != "unknown":
            return level
    score = raw.get("regime_score", raw.get("market_regime_score"))
    if score is None and nested:
        score = nested.get("score")
    numeric_score = coerce_number(score, None)
    if numeric_score is None:
        return "unknown"
    if numeric_score >= 68:
        return "risk_on"
    if numeric_score <= 41:
        return "risk_off"
    return "balanced"


def _normalize_regime_level(value) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("-", "_").replace(" ", "_")
    if text in {"risk_on", "on", "bull", "bullish"}:
        return "risk_on"
    if text in {"risk_off", "off", "bear", "bearish", "defensive"}:
        return "risk_off"
    if text in {"balanced", "neutral", "normal"}:
        return "balanced"
    return "unknown"


def _fit_interaction_terms(
    strategy: str,
    samples: List[Dict[str, object]],
    max_pairs: int = None,
) -> Dict[str, object]:
    min_samples = int(getattr(config, "INTERACTION_MIN_TRAIN_SAMPLES", 20))
    max_pairs = int(max_pairs or getattr(config, "INTERACTION_TERM_MAX_PAIRS", 8))
    score_scale = coerce_number(getattr(config, "INTERACTION_SCORE_SCALE", 6.0), 6.0)
    delta_cap = coerce_number(getattr(config, "INTERACTION_SCORE_DELTA_CAP", 12.0), 12.0)
    result = {
        "strategy": strategy,
        "sample_count": len(samples),
        "score_scale": score_scale,
        "delta_cap": delta_cap,
        "interactions": [],
    }
    if len(samples) < min_samples:
        result["status"] = "insufficient_train_samples"
        result["min_samples"] = min_samples
        return result

    day_returns: Dict[str, List[float]] = {}
    for sample in samples:
        date_key = str(sample.get("signal_date") or "")
        day_returns.setdefault(date_key, []).append(coerce_number(sample.get("primary_return_net")))
    day_medians = {date_key: _median(values) for date_key, values in day_returns.items()}
    candidate_pairs = _interaction_candidate_pairs(strategy)
    preferred_rank = {pair: index for index, pair in enumerate(candidate_pairs)}
    min_abs_corr = max(0.0, coerce_number(getattr(config, "INTERACTION_MIN_ABS_CORR", 0.02), 0.02))
    fitted = []
    for left, right in candidate_pairs:
        xs = []
        ys = []
        for sample in samples:
            components = _live_components(sample.get("raw") or {})
            x_value = _interaction_feature_product(components, left, right)
            target = coerce_number(sample.get("primary_return_net")) - day_medians.get(str(sample.get("signal_date") or ""), 0.0)
            xs.append(x_value)
            ys.append(target)
        if len(xs) < min_samples:
            continue
        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)
        var_x = sum((value - mean_x) ** 2 for value in xs) / len(xs)
        var_y = sum((value - mean_y) ** 2 for value in ys) / len(ys)
        if var_x <= 1e-10 or var_y <= 1e-10:
            continue
        cov_xy = sum((x_value - mean_x) * (y_value - mean_y) for x_value, y_value in zip(xs, ys)) / len(xs)
        corr = cov_xy / math.sqrt(var_x * var_y)
        if abs(corr) < min_abs_corr:
            continue
        coefficient = max(-5.0, min(5.0, cov_xy / var_x))
        fitted.append(
            {
                "pair": "{}*{}".format(left, right),
                "left": left,
                "right": right,
                "coefficient": round(coefficient, 6),
                "correlation": round(corr, 6),
                "sample_count": len(xs),
                "preferred_rank": preferred_rank.get((left, right), 9999),
            }
        )
    fitted.sort(key=lambda item: (-abs(coerce_number(item.get("correlation"))), int(item.get("preferred_rank", 9999)), item["pair"]))
    selected = fitted[: max(0, max_pairs)]
    for item in selected:
        item.pop("preferred_rank", None)
    result["status"] = "ready" if selected else "no_stable_interactions"
    result["interactions"] = selected
    return result


def _interaction_candidate_pairs(strategy: str) -> List[tuple]:
    keys = _interaction_feature_keys(strategy)
    available = set(keys)
    pairs = []
    seen = set()

    def add_pair(left: str, right: str):
        if left == right or left not in available or right not in available:
            return
        marker = frozenset((left, right))
        if marker in seen:
            return
        seen.add(marker)
        pairs.append((left, right))

    for left, right in (
        ("momentum_score", "liquidity_score"),
        ("execution_score", "tail_setup_score"),
        ("historical_edge_score", "momentum_score"),
        ("liquidity_score", "tail_setup_score"),
        ("momentum_score", "risk_penalty"),
    ):
        add_pair(left, right)
    for left, right in itertools.combinations(keys, 2):
        add_pair(left, right)
    return pairs


def _interaction_feature_keys(strategy: str) -> List[str]:
    keys: List[str] = []
    for term in STRATEGY_COMBINERS[strategy]["terms"]:
        key = str(term["component"])
        if key not in keys:
            keys.append(key)
    if "risk_penalty" not in keys:
        keys.append("risk_penalty")
    return keys


def _interaction_feature_product(components: Dict[str, object], left: str, right: str) -> float:
    return _interaction_feature_value(components, left) * _interaction_feature_value(components, right)


def _interaction_feature_value(components: Dict[str, object], key: str) -> float:
    if key == "risk_penalty":
        value = coerce_number(components.get(key), 0.0)
        return max(-1.0, min(1.0, (value - 8.0) / 12.0))
    value = coerce_number(components.get(key), 50.0)
    return max(-1.0, min(1.0, (value - 50.0) / 50.0))


def _interaction_score_delta(components: Dict[str, object], model: Dict[str, object]) -> float:
    interactions = model.get("interactions") if isinstance(model, dict) else []
    if not interactions:
        return 0.0
    score_scale = coerce_number(model.get("score_scale"), coerce_number(getattr(config, "INTERACTION_SCORE_SCALE", 6.0), 6.0))
    delta_cap = max(0.0, coerce_number(model.get("delta_cap"), coerce_number(getattr(config, "INTERACTION_SCORE_DELTA_CAP", 12.0), 12.0)))
    delta = 0.0
    for item in interactions:
        left = str(item.get("left") or "")
        right = str(item.get("right") or "")
        if not left or not right:
            continue
        delta += coerce_number(item.get("coefficient")) * _interaction_feature_product(components, left, right) * score_scale
    return max(-delta_cap, min(delta_cap, delta))


def _calibrate_fdr_result(
    positive_folds: int,
    fold_count: int,
    increments: List[object] = None,
    experiment_id: str = "",
) -> Dict[str, object]:
    increments = [coerce_number(value) for value in (increments or []) if value is not None]
    p_value = (
        moving_block_bootstrap_positive_mean_p_value(increments)
        if len(increments) >= 2
        else _binomial_tail_probability(positive_folds, fold_count)
    )
    q = coerce_number(getattr(config, "CALIBRATE_FDR_Q", 0.1), 0.1)
    enabled = bool(getattr(config, "ENABLE_CALIBRATE_FDR", False))
    candidate_id = str(experiment_id or "runtime_candidate")
    records = []
    try:
        records = list_experiments()
    except Exception:
        records = []
    records = [record for record in records if isinstance(record, dict)]
    records.append(
        {
            "experiment_id": candidate_id,
            "experiment_family": "runtime_calibration",
            "trial_count": 1,
            "result": {"p_value": p_value},
        }
    )
    family_fdr = unified_experiment_fdr(records, q=q)
    passed = (not enabled) or candidate_id in set(family_fdr.get("rejected_experiment_ids") or [])
    dsr = deflated_sharpe_ratio(
        increments,
        trial_count=max(1, int(sum(int(record.get("trial_count") or 1) for record in records))),
        probability_threshold=coerce_number(getattr(config, "EXPERIMENT_DSR_MIN_PROBABILITY", 0.95), 0.95),
    )
    return {
        "enabled": enabled,
        "method": "unified_bh_fdr",
        "scope": "full_experiment_family",
        "q": q,
        "p_value": round(coerce_number(p_value, 1.0), 8),
        "passed": passed,
        "status": "passed" if passed else "blocked",
        "tested_count": family_fdr.get("tested_count", 0),
        "rejected_experiment_ids": family_fdr.get("rejected_experiment_ids", []),
        "adjusted_p_values": family_fdr.get("adjusted_p_values", []),
        "dsr": dsr,
    }


def _mean_ci(values: List[object], z_score: float = 1.96) -> tuple:
    numeric = [coerce_number(value, None) for value in values or []]
    numeric = [value for value in numeric if value is not None]
    if not numeric:
        return None, None
    mean = sum(numeric) / len(numeric)
    if len(numeric) == 1:
        return round(mean, 4), round(mean, 4)
    stdev = statistics.stdev(numeric)
    margin = z_score * stdev / math.sqrt(len(numeric))
    return round(mean - margin, 4), round(mean + margin, 4)


def benjamini_hochberg_fdr(p_values: List[object], q: float = 0.1) -> Dict[str, object]:
    return _benjamini_hochberg_fdr(p_values, q=q)


def calibrate_with_fdr_guard(
    candidate_configs: List[Dict[str, object]],
    evaluate_fn,
    q: float = None,
) -> Dict[str, object]:
    evaluated = []
    for candidate in candidate_configs or []:
        metrics = evaluate_fn(candidate)
        metrics = metrics if isinstance(metrics, dict) else {}
        evaluated.append({"config": candidate, "metrics": metrics, "p_value": coerce_number(metrics.get("p_value"), 1.0)})
    fdr_result = benjamini_hochberg_fdr(
        [item["p_value"] for item in evaluated],
        q=coerce_number(q, coerce_number(getattr(config, "CALIBRATE_FDR_Q", 0.1), 0.1)),
    )
    significant = [evaluated[index] for index in fdr_result.get("rejected", []) if 0 <= index < len(evaluated)]
    if not significant:
        return {
            "selected": None,
            "status": "no_significant_config",
            "evaluated": evaluated,
            "fdr": fdr_result,
        }
    selected = max(significant, key=lambda item: coerce_number(item["metrics"].get("objective"), -1e9))
    return {
        "selected": selected["config"],
        "status": "selected",
        "metrics": selected["metrics"],
        "evaluated": evaluated,
        "fdr": fdr_result,
    }


def _binomial_tail_probability(successes: int, trials: int) -> float:
    trials = max(0, int(trials or 0))
    if trials <= 0:
        return 1.0
    successes = max(0, min(trials, int(successes or 0)))
    favorable = sum(math.comb(trials, k) for k in range(successes, trials + 1))
    return favorable / float(2**trials)


def _metrics_from_ranked_groups(grouped: Dict[str, List[Dict[str, object]]], top_k: int) -> Dict[str, object]:
    selected: List[Dict[str, object]] = []
    for rows in grouped.values():
        benchmark = _median([coerce_number(item.get("primary_return_net")) for item in rows])
        for item in rows:
            item["benchmark_return_net"] = benchmark
            item["excess_return_net"] = coerce_number(item.get("primary_return_net")) - benchmark
        ranked = sorted(rows, key=lambda item: coerce_number(item.get("recomputed_score")), reverse=True)
        selected.extend(ranked[: max(1, int(top_k))])
    if not selected:
        return {
            "sample_count": 0,
            "win_rate": 0.0,
            "avg_period_return": 0.0,
            "absolute_win_rate": 0.0,
            "absolute_avg_period_return": 0.0,
            "return_series": [],
            "return_series_with_dates": [],
        }
    wins = [coerce_number(sample.get("excess_return_net")) > 0 for sample in selected]
    absolute_wins = [coerce_number(sample.get("primary_return_net")) > 0 for sample in selected]
    period_returns = [coerce_number(sample["primary_return_net"]) for sample in selected]
    return_series_with_dates = [(str(sample.get("signal_date") or ""), coerce_number(sample.get("primary_return_net"))) for sample in selected]
    next_open_returns = [coerce_number(sample.get("next_open_return")) for sample in selected]
    max_drawdowns = [coerce_number(sample.get("max_drawdown")) for sample in selected]
    avg_return = sum(period_returns) / len(period_returns)
    avg_excess = sum(coerce_number(sample.get("excess_return_net")) for sample in selected) / len(selected)
    win_count = sum(1 for value in absolute_wins if value)
    loss_count = len(absolute_wins) - win_count
    return {
        "sample_count": len(selected),
        "candidate_sample_count": sum(len(rows) for rows in grouped.values()),
        "day_count": len(grouped),
        "win_rate": round(sum(1 for value in wins if value) / len(wins) * 100, 2),
        "avg_period_return": round(avg_excess, 4),
        "absolute_win_count": win_count,
        "absolute_loss_count": loss_count,
        "absolute_win_rate": round(sum(1 for value in absolute_wins if value) / len(absolute_wins) * 100, 2),
        "absolute_avg_period_return": round(avg_return, 4),
        "absolute_median_period_return": round(_median(period_returns), 4),
        "absolute_loss_quantile_return": round(_quantile(period_returns, 0.25), 4),
        "absolute_avg_next_open_return": round(sum(next_open_returns) / len(next_open_returns), 4),
        "absolute_avg_max_drawdown": round(sum(max_drawdowns) / len(max_drawdowns), 4),
        "absolute_max_drawdown": round(min(max_drawdowns), 4) if max_drawdowns else 0.0,
        "return_series": [round(value, 4) for value in period_returns],
        "return_series_with_dates": return_series_with_dates,
        "sortino": _sortino_ratio(period_returns, avg_return),
    }



def _fit_weights(
    strategy: str,
    samples: List[Dict[str, object]],
    top_k: int,
    steps: int,
    initial: Dict[str, float] = None,
    direction_focus=None,
) -> Dict[str, object]:
    best = _normalize_strategy_weights(strategy, copy.deepcopy(initial or _current_strategy_weights(strategy)))
    best_metrics = _evaluate_live_samples(strategy, samples, best, top_k=top_k)
    best_obj = _objective(best_metrics, strategy, direction_focused=_resolve_tomorrow_direction_focus(direction_focus))
    multipliers = (0.7, 1.0, 1.3)
    keys = _strategy_weight_keys(strategy)
    for _ in range(max(1, steps)):
        improved = False
        for key in keys:
            for mult in multipliers:
                if mult == 1.0:
                    continue
                candidate = copy.deepcopy(best)
                candidate[key] = coerce_number(candidate.get(key), 0.0) * mult
                candidate = _normalize_strategy_weights(strategy, candidate)
                metrics = _evaluate_live_samples(strategy, samples, candidate, top_k=top_k)
                obj = _objective(metrics, strategy, direction_focused=_resolve_tomorrow_direction_focus(direction_focus))
                if obj > best_obj + 1e-9:
                    best, best_obj, best_metrics, improved = candidate, obj, metrics, True
        if not improved:
            break
    return {"weights": best, "metrics": best_metrics, "objective": best_obj}


def _walk_forward_evaluate(
    strategy: str,
    samples: List[Dict[str, object]],
    current: Dict[str, float],
    top_k: int,
    steps: int,
    direction_focus=None,
) -> Dict[str, object]:
    folds = _walk_forward_splits(
        samples,
        int(getattr(config, "CALIBRATE_WALK_FORWARD_FOLDS", 4)),
        purge_days=_strategy_oos_purge_days(strategy),
    )
    if not folds:
        return {"ok": False, "status": "insufficient_oos_folds", "folds": [], "fold_count": 0}
    rows = []
    for index, (train_samples, test_samples, train_dates, test_dates) in enumerate(folds, start=1):
        fitted = _fit_weights(
            strategy,
            train_samples,
            top_k=top_k,
            steps=steps,
            initial=current,
            direction_focus=direction_focus,
        )
        baseline_metrics = _evaluate_live_samples(strategy, test_samples, current, top_k=top_k)
        best_metrics = _evaluate_live_samples(strategy, test_samples, fitted["weights"], top_k=top_k)
        baseline_obj = _objective(baseline_metrics, strategy, direction_focused=_resolve_tomorrow_direction_focus(direction_focus))
        best_obj = _objective(best_metrics, strategy, direction_focused=_resolve_tomorrow_direction_focus(direction_focus))
        rows.append(
            {
                "fold": index,
                "train_start": train_dates[0],
                "train_end": train_dates[-1],
                "test_start": test_dates[0],
                "test_end": test_dates[-1],
                "train_sample_count": len(train_samples),
                "test_sample_count": len(test_samples),
                "is_objective": round(coerce_number(fitted["objective"]), 4),
                "baseline_oos_objective": round(baseline_obj, 4),
                "best_oos_objective": round(best_obj, 4),
                "oos_improvement": round(best_obj - baseline_obj, 4),
            }
        )
    baseline_values = [coerce_number(row["baseline_oos_objective"]) for row in rows]
    best_values = [coerce_number(row["best_oos_objective"]) for row in rows]
    improvements = [coerce_number(row["oos_improvement"]) for row in rows]
    return {
        "ok": True,
        "status": "ok",
        "folds": rows,
        "fold_count": len(rows),
        "positive_folds": sum(1 for value in improvements if value > 0),
        "baseline_oos_objective": round(sum(baseline_values) / len(baseline_values), 4),
        "best_oos_objective": round(sum(best_values) / len(best_values), 4),
        "oos_improvement": round(sum(improvements) / len(improvements), 4),
    }


def _walk_forward_splits(
    samples: List[Dict[str, object]],
    requested_folds: int,
    purge_days: int = 0,
) -> List[tuple]:
    dates = sorted({str(sample.get("signal_date")) for sample in samples if sample.get("signal_date")})
    if len(dates) < 3:
        return []
    fold_count = min(max(1, requested_folds), len(dates) - 1)
    test_size = max(1, len(dates) // (fold_count + 1))
    purge_days = max(0, int(coerce_number(purge_days, 0)))
    splits = []
    for fold_index in range(fold_count):
        train_end = len(dates) - test_size * (fold_count - fold_index)
        test_end = min(len(dates), train_end + test_size)
        if train_end <= 0 or test_end <= train_end:
            continue
        purged_train_end = max(0, train_end - purge_days)
        train_dates = dates[:purged_train_end]
        test_dates = dates[train_end:test_end]
        if not train_dates:
            continue
        train_set = set(train_dates)
        test_set = set(test_dates)
        train_samples = [sample for sample in samples if str(sample.get("signal_date")) in train_set]
        test_samples = [sample for sample in samples if str(sample.get("signal_date")) in test_set]
        if train_samples and test_samples:
            splits.append((train_samples, test_samples, train_dates, test_dates))
    return splits


def _strategy_oos_purge_days(strategy: str) -> int:
    strategy = str(strategy or "")
    if strategy == "swing_picks":
        return 5
    if strategy in {"tomorrow_picks", "today_term"}:
        return 1
    return 0


def _strategy_weight_keys(strategy: str) -> List[str]:
    keys: List[str] = []
    for term in STRATEGY_COMBINERS[strategy]["terms"]:
        key = str(term["weight_key"])
        if key not in keys:
            keys.append(key)
    return keys


def _current_strategy_weights(strategy: str) -> Dict[str, float]:
    source = copy.deepcopy(WEIGHTS.get(strategy, {}))
    return _normalize_strategy_weights(strategy, source)


def _normalize_strategy_weights(strategy: str, values: Dict[str, object]) -> Dict[str, float]:
    keys = _strategy_weight_keys(strategy)
    if not keys:
        return {}
    clamped = {}
    for key in keys:
        value = coerce_number(values.get(key), coerce_number(WEIGHTS.get(strategy, {}).get(key), 0.0))
        clamped[key] = max(0.01, min(0.75, value))
    total = sum(clamped.values())
    if total <= 0:
        default = 1.0 / len(keys)
        return {key: round(default, 4) for key in keys}
    return {key: round(value / total, 4) for key, value in clamped.items()}


def _average_sample_coverage(samples: List[Dict[str, object]]) -> float:
    if not samples:
        return 0.0
    values = []
    for sample in samples:
        raw = sample.get("raw") or {}
        profile = raw.get("serenity_profile") if isinstance(raw.get("serenity_profile"), dict) else {}
        if profile and profile.get("data_coverage") is not None:
            values.append(max(0.0, min(1.0, coerce_number(profile.get("data_coverage")))))
            continue
        present = 0
        for column in (
            "ret_3d",
            "ret_5d",
            "ret_10d",
            "ret_20d",
            "ma5_gap",
            "ma20_gap",
            "vol_amount_5d",
            "breakout_20d",
            "volatility_20d",
        ):
            if abs(coerce_number(raw.get(column))) > 1e-9:
                present += 1
        values.append(present / 9.0)
    return sum(values) / len(values)


def _sample_has_live_components(strategy: str, raw: Dict[str, object]) -> bool:
    components = _live_components(raw)
    required = [str(term["component"]) for term in STRATEGY_COMBINERS[strategy]["terms"]]
    present = sum(1 for key in required if key in components)
    return present >= max(1, int(len(required) * 0.6))


def _live_components(raw: Dict[str, object]) -> Dict[str, object]:
    components = dict(raw or {})
    if "oversold_calm_score" not in components:
        parts = []
        for key in ("reversal_score", "lowvol_score", "not_overextended_score"):
            if key in components:
                parts.append(coerce_number(components.get(key)))
        if parts:
            components["oversold_calm_score"] = sum(parts) / len(parts)
    if "volume_break_score" not in components and "volume_score" in components:
        components["volume_break_score"] = components.get("volume_score")
    if "risk_penalty" not in components:
        parts = components.get("risk_penalty_parts") or {}
        if isinstance(parts, dict):
            components["risk_penalty"] = sum(coerce_number(value) for value in parts.values())
        else:
            components["risk_penalty"] = 0.0
    components.setdefault("regime_bonus", 0.0)
    return components


def _evaluate_live_samples(
    strategy: str,
    samples: List[Dict[str, object]],
    weights: Dict[str, float],
    top_k: int,
) -> Dict[str, object]:
    grouped: Dict[str, List[Dict[str, object]]] = {}
    for sample in samples:
        raw = sample.get("raw") or {}
        components = _live_components(raw)
        combined = _combine_details(
            components,
            strategy,
            weights={**WEIGHTS, strategy: weights},
            row=pd.Series(raw),
            regime_weight_profile=raw.get("regime_weight_profile") or {},
        )
        enriched = dict(sample)
        enriched["recomputed_score"] = combined["score"]
        grouped.setdefault(str(sample.get("signal_date")), []).append(enriched)
    selected: List[Dict[str, object]] = []
    for rows in grouped.values():
        benchmark = _median([coerce_number(item.get("primary_return_net")) for item in rows])
        for item in rows:
            item["benchmark_return_net"] = benchmark
            item["excess_return_net"] = coerce_number(item.get("primary_return_net")) - benchmark
        ranked = sorted(rows, key=lambda item: item["recomputed_score"], reverse=True)
        selected.extend(ranked[: max(1, int(top_k))])
    if not selected:
        return {
            "sample_count": 0,
            "win_rate": 0.0,
            "avg_period_return": 0.0,
            "absolute_win_rate": 0.0,
            "absolute_avg_period_return": 0.0,
            "return_series": [],
            "return_series_with_dates": [],
        }
    wins = [coerce_number(sample.get("excess_return_net")) > 0 for sample in selected]
    absolute_wins = [coerce_number(sample.get("primary_return_net")) > 0 for sample in selected]
    period_returns = [coerce_number(sample["primary_return_net"]) for sample in selected]
    return_series_with_dates = [(str(sample.get("signal_date") or ""), coerce_number(sample.get("primary_return_net"))) for sample in selected]
    next_open_returns = [coerce_number(sample.get("next_open_return")) for sample in selected]
    max_drawdowns = [coerce_number(sample.get("max_drawdown")) for sample in selected]
    avg_return = sum(period_returns) / len(period_returns)
    avg_excess = sum(coerce_number(sample.get("excess_return_net")) for sample in selected) / len(selected)
    win_count = sum(1 for value in absolute_wins if value)
    loss_count = len(absolute_wins) - win_count
    return {
        "sample_count": len(selected),
        "candidate_sample_count": len(samples),
        "day_count": len(grouped),
        "win_rate": round(sum(1 for value in wins if value) / len(wins) * 100, 2),
        "avg_period_return": round(avg_excess, 4),
        "absolute_win_count": win_count,
        "absolute_loss_count": loss_count,
        "absolute_win_rate": round(sum(1 for value in absolute_wins if value) / len(absolute_wins) * 100, 2),
        "absolute_avg_period_return": round(avg_return, 4),
        "absolute_median_period_return": round(_median(period_returns), 4),
        "absolute_loss_quantile_return": round(_quantile(period_returns, 0.25), 4),
        "absolute_avg_next_open_return": round(sum(next_open_returns) / len(next_open_returns), 4),
        "absolute_avg_max_drawdown": round(sum(max_drawdowns) / len(max_drawdowns), 4),
        "absolute_max_drawdown": round(min(max_drawdowns), 4) if max_drawdowns else 0.0,
        "return_series": [round(value, 4) for value in period_returns],
        "return_series_with_dates": return_series_with_dates,
        "sortino": _sortino_ratio(period_returns, avg_return),
    }


def _median(values: List[float]) -> float:
    clean = sorted(coerce_number(value) for value in values)
    if not clean:
        return 0.0
    mid = len(clean) // 2
    if len(clean) % 2:
        return clean[mid]
    return (clean[mid - 1] + clean[mid]) / 2.0


def _quantile(values: List[float], q: float) -> float:
    clean = sorted(coerce_number(value) for value in values)
    if not clean:
        return 0.0
    q = max(0.0, min(1.0, coerce_number(q)))
    index = int((len(clean) - 1) * q)
    return clean[index]


def main() -> int:
    parser = argparse.ArgumentParser(description="AlphaLite 信号权重离线回测校准")
    parser.add_argument("--codes", default="", help="逗号分隔的股票代码；留空则用历史缓存里全部代码")
    parser.add_argument("--fetch", action="store_true", help="缓存缺失时允许抓取历史（触网）")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--holding-days", type=int, default=3)
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--steps", type=int, default=2, help="坐标下降轮数")
    parser.add_argument("--compare-momentum", action="store_true", help="对比动量vs反转倾向，据此建议/写入 reversal_tilt")
    parser.add_argument("--calibrate-live-weights", default="", help="用真实验证样本校准上线策略权重；可填策略名或 all")
    parser.add_argument("--validation-days", type=int, default=120, help="live 权重校准读取最近多少个信号日")
    parser.add_argument("--dry-run", action="store_true", help="只评估并打印，不写 weights.json")
    args = parser.parse_args()

    if args.calibrate_live_weights.strip():
        target = args.calibrate_live_weights.strip()
        strategies = list(STRATEGY_COMBINERS) if target == "all" else [target]
        exit_code = 0
        for strategy in strategies:
            result = calibrate_live_weights(
                strategy,
                top_k=args.top_k,
                days=args.validation_days,
                steps=args.steps,
                dry_run=args.dry_run,
            )
            print(
                "{}: status={} samples={} is_base={} is_best={} is_improve={} oos_base={} oos_best={} oos_improve={} folds={}/{}".format(
                    strategy,
                    result.get("status"),
                    result.get("sample_count", 0),
                    result.get("baseline_objective", "-"),
                    result.get("best_objective", "-"),
                    result.get("improvement", "-"),
                    result.get("oos_baseline_objective", "-"),
                    result.get("oos_best_objective", "-"),
                    result.get("oos_improvement", "-"),
                    result.get("oos_positive_folds", 0),
                    result.get("oos_fold_count", 0),
                )
            )
            if result.get("weights"):
                print("  weights={}".format(json.dumps(result["weights"], ensure_ascii=False)))
            if not result.get("ok"):
                exit_code = 1
        return exit_code

    if args.codes.strip():
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        codes = _cached_codes(config.HISTORY_CACHE_PATH)

    if not codes:
        print("没有可用代码：历史缓存为空，请用 --codes 指定并加 --fetch 抓取。")
        return 1

    history_by_code = _load_history(codes, fetch=args.fetch)
    if not history_by_code:
        print("没有可用历史数据，无法回测。可加 --fetch 抓取，或先运行应用预热缓存。")
        return 1

    if args.compare_momentum:
        cmp = compare_momentum(
            history_by_code,
            top_k=args.top_k,
            holding_days=args.holding_days,
            lookback_days=args.lookback_days,
        )
        print("样本代码数：{}".format(len(history_by_code)))
        print("动量倾向 目标={} 指标={}".format(cmp["momentum_objective"], cmp["momentum_metrics"]))
        print("反转倾向 目标={} 指标={}".format(cmp["reversal_objective"], cmp["reversal_metrics"]))
        print("反转更优：{}  建议 reversal_tilt={}".format(cmp["reversal_wins"], cmp["suggested_reversal_tilt"]))
        if args.dry_run:
            print("(dry-run) 未写入 weights.json")
            return 0
        if not cmp["reversal_wins"]:
            print("回测显示动量不劣于反转，保持 reversal_tilt=0，不覆盖。")
            return 0
        _write_weights_override(
            {"weights": {"today_term": {"reversal_tilt": cmp["suggested_reversal_tilt"]}}}
        )
        print("已写入 reversal_tilt={} 到 {}".format(cmp["suggested_reversal_tilt"], config.WEIGHTS_OVERRIDE_PATH))
        return 0

    result = calibrate(
        history_by_code,
        top_k=args.top_k,
        holding_days=args.holding_days,
        lookback_days=args.lookback_days,
        steps=args.steps,
    )

    print("样本代码数：{}".format(len(history_by_code)))
    print("基线目标：{}  基线指标：{}".format(result["baseline_objective"], result["baseline_metrics"]))
    print("最优目标：{}  最优指标：{}".format(result["best_objective"], result["best_metrics"]))
    print("最优权重：{}".format(json.dumps(result["weights"], ensure_ascii=False)))

    if args.dry_run:
        print("(dry-run) 未写入 weights.json")
        return 0

    if result["best_objective"] <= result["baseline_objective"] + 1e-9:
        print("未找到比默认更优的权重，保持现状，不覆盖。")
        return 0

    _write_weights_override({"alphalite_signal": result["weights"]})
    print("已写入 {}".format(config.WEIGHTS_OVERRIDE_PATH))
    return 0


def _write_weights_override(patch: Dict[str, object]) -> None:
    """把 patch 深合并进 .runtime/weights.json（保留其它段）。"""
    path = config.WEIGHTS_OVERRIDE_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            payload = {}
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(payload.get(key), dict):
            for sub_k, sub_v in value.items():
                if isinstance(sub_v, dict) and isinstance(payload[key].get(sub_k), dict):
                    payload[key][sub_k].update(sub_v)
                else:
                    payload[key][sub_k] = sub_v
        else:
            payload[key] = value
    atomic_write_json(path, payload, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    raise SystemExit(main())
