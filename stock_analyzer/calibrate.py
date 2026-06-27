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
import json
import os
import sqlite3
from typing import Dict, List

import pandas as pd

from . import config
from .backtest import _DEFAULT_ALPHALITE_WEIGHTS, run_rolling_alphalite_backtest
from .history_cache import HistoryCache
from .normalization import coerce_number, normalize_code
from .scoring import STRATEGY_COMBINERS, WEIGHTS, _combine_details
from .strategy_validation import StrategyValidationStore


def _cached_codes(db_path: str) -> List[str]:
    if not os.path.exists(db_path):
        return []
    with sqlite3.connect(db_path) as conn:
        try:
            rows = conn.execute("SELECT DISTINCT code FROM daily_history").fetchall()
        except sqlite3.OperationalError:
            return []
    return [str(row[0]) for row in rows if row and row[0]]


def _load_history(codes: List[str], fetch: bool) -> Dict[str, pd.DataFrame]:
    cache = HistoryCache(config.HISTORY_CACHE_PATH, config.HISTORY_CACHE_FRESHNESS_HOURS)
    history_by_code: Dict[str, pd.DataFrame] = {}
    provider = None
    if fetch:
        # 仅在显式要求时才创建 provider 抓取，避免离线运行触网。
        from .providers import MarketDataProvider

        provider = MarketDataProvider()
    for code in codes:
        code = normalize_code(code)
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


def _objective(metrics: Dict[str, object]) -> float:
    """目标函数：命中率为主，平均收益为辅。无有效交易则极低分。"""
    if not metrics:
        return -1e9
    win_rate = float(metrics.get("win_rate", 0.0) or 0.0)
    avg_return = float(metrics.get("avg_period_return", 0.0) or 0.0)
    return win_rate + avg_return * 2.0


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
    （动量）vs 取负（反转），看哪套 win_rate+收益更好，据此建议 short_term 的
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
) -> Dict[str, object]:
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
    baseline_metrics = _evaluate_live_samples(strategy, samples, current, top_k=top_k)
    baseline_obj = _objective(baseline_metrics)
    fitted = _fit_weights(strategy, samples, top_k=top_k, steps=steps, initial=current)
    best = fitted["weights"]
    best_metrics = fitted["metrics"]
    best_obj = fitted["objective"]
    walk_forward = _walk_forward_evaluate(strategy, samples, current, top_k=top_k, steps=steps)

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
        "improvement": round(best_obj - baseline_obj, 4),
        "oos_baseline_objective": walk_forward.get("baseline_oos_objective"),
        "oos_best_objective": walk_forward.get("best_oos_objective"),
        "oos_improvement": walk_forward.get("oos_improvement"),
        "oos_positive_folds": walk_forward.get("positive_folds", 0),
        "oos_fold_count": walk_forward.get("fold_count", 0),
        "folds": walk_forward.get("folds", []),
        "weights": patch,
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
    result["status"] = "written"
    result["path"] = config.WEIGHTS_OVERRIDE_PATH
    return result


def _fit_weights(
    strategy: str,
    samples: List[Dict[str, object]],
    top_k: int,
    steps: int,
    initial: Dict[str, float] = None,
) -> Dict[str, object]:
    best = _normalize_strategy_weights(strategy, copy.deepcopy(initial or _current_strategy_weights(strategy)))
    best_metrics = _evaluate_live_samples(strategy, samples, best, top_k=top_k)
    best_obj = _objective(best_metrics)
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
                obj = _objective(metrics)
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
) -> Dict[str, object]:
    folds = _walk_forward_splits(samples, int(getattr(config, "CALIBRATE_WALK_FORWARD_FOLDS", 4)))
    if not folds:
        return {"ok": False, "status": "insufficient_oos_folds", "folds": [], "fold_count": 0}
    rows = []
    for index, (train_samples, test_samples, train_dates, test_dates) in enumerate(folds, start=1):
        fitted = _fit_weights(strategy, train_samples, top_k=top_k, steps=steps, initial=current)
        baseline_metrics = _evaluate_live_samples(strategy, test_samples, current, top_k=top_k)
        best_metrics = _evaluate_live_samples(strategy, test_samples, fitted["weights"], top_k=top_k)
        baseline_obj = _objective(baseline_metrics)
        best_obj = _objective(best_metrics)
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
) -> List[tuple]:
    dates = sorted({str(sample.get("signal_date")) for sample in samples if sample.get("signal_date")})
    if len(dates) < 3:
        return []
    fold_count = min(max(1, requested_folds), len(dates) - 1)
    test_size = max(1, len(dates) // (fold_count + 1))
    splits = []
    for fold_index in range(fold_count):
        train_end = len(dates) - test_size * (fold_count - fold_index)
        test_end = min(len(dates), train_end + test_size)
        if train_end <= 0 or test_end <= train_end:
            continue
        train_dates = dates[:train_end]
        test_dates = dates[train_end:test_end]
        train_set = set(train_dates)
        test_set = set(test_dates)
        train_samples = [sample for sample in samples if str(sample.get("signal_date")) in train_set]
        test_samples = [sample for sample in samples if str(sample.get("signal_date")) in test_set]
        if train_samples and test_samples:
            splits.append((train_samples, test_samples, train_dates, test_dates))
    return splits


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
        }
    wins = [coerce_number(sample.get("excess_return_net")) > 0 for sample in selected]
    absolute_wins = [coerce_number(sample.get("primary_return_net")) > 0 for sample in selected]
    avg_return = sum(coerce_number(sample["primary_return_net"]) for sample in selected) / len(selected)
    avg_excess = sum(coerce_number(sample.get("excess_return_net")) for sample in selected) / len(selected)
    return {
        "sample_count": len(selected),
        "candidate_sample_count": len(samples),
        "day_count": len(grouped),
        "win_rate": round(sum(1 for value in wins if value) / len(wins) * 100, 2),
        "avg_period_return": round(avg_excess, 4),
        "absolute_win_rate": round(sum(1 for value in absolute_wins if value) / len(absolute_wins) * 100, 2),
        "absolute_avg_period_return": round(avg_return, 4),
    }


def _median(values: List[float]) -> float:
    clean = sorted(coerce_number(value) for value in values)
    if not clean:
        return 0.0
    mid = len(clean) // 2
    if len(clean) % 2:
        return clean[mid]
    return (clean[mid - 1] + clean[mid]) / 2.0


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
            {"weights": {"short_term": {"reversal_tilt": cmp["suggested_reversal_tilt"]}}}
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
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    raise SystemExit(main())
