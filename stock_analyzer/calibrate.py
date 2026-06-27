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
from .normalization import normalize_code


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


def main() -> int:
    parser = argparse.ArgumentParser(description="AlphaLite 信号权重离线回测校准")
    parser.add_argument("--codes", default="", help="逗号分隔的股票代码；留空则用历史缓存里全部代码")
    parser.add_argument("--fetch", action="store_true", help="缓存缺失时允许抓取历史（触网）")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--holding-days", type=int, default=3)
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--steps", type=int, default=2, help="坐标下降轮数")
    parser.add_argument("--compare-momentum", action="store_true", help="对比动量vs反转倾向，据此建议/写入 reversal_tilt")
    parser.add_argument("--dry-run", action="store_true", help="只评估并打印，不写 weights.json")
    args = parser.parse_args()

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
