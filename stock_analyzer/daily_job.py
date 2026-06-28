import argparse
import json
import os
from datetime import datetime

from . import config
from .strategy_validation import StrategyValidationStore


def _save_iteration_payload(result, applied=False):
    from .calibrate import _current_strategy_weights

    path = getattr(config, "TOMORROW_ITERATION_PATH", ".runtime/tomorrow_iteration.json")
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "strategy": "tomorrow_picks",
        "days": 120,
        "current_weights": _current_strategy_weights("tomorrow_picks"),
        "suggested_weights": result.get("weights") or {},
        "can_apply": bool(result.get("ok")) and result.get("status") == "dry_run_improved",
        "applied": applied,
        "reason": result.get("status", ""),
        "result": result,
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="盘后每日任务：保存策略快照并回填验证结果")
    parser.add_argument("--snapshot", action="store_true", help="保存策略当日预测快照")
    parser.add_argument("--update", action="store_true", help="回填已保存快照的未来收益")
    parser.add_argument("--paper-trade", action="store_true", help="根据最近保存快照生成/更新纸面组合交易")
    parser.add_argument("--factor-ic", action="store_true", help="基于真实验证样本刷新因子 IC 文件")
    parser.add_argument("--calibrate-live", action="store_true", help="基于明天预测真实验证样本校准权重")
    parser.add_argument("--write-weights", action="store_true", help="与 --calibrate-live 配合，确认写入 weights.json")
    parser.add_argument("--strategy", default="tomorrow_picks", help="当前只支持 tomorrow_picks；all 会映射为 tomorrow_picks")
    parser.add_argument("--market", default="all", choices=("all", "main", "chinext", "star"))
    args = parser.parse_args()

    if not args.snapshot and not args.update and not args.paper_trade and not args.factor_ic and not args.calibrate_live:
        parser.error("至少指定 --snapshot、--update、--paper-trade、--factor-ic 或 --calibrate-live")

    from .paper_trading import PaperTradingStore
    from .providers import MarketDataProvider
    from .snapshot import run_snapshots
    from .strategy_validation import StrategyValidationStore

    provider = MarketDataProvider()
    store = StrategyValidationStore(config.VALIDATION_DB_PATH)
    paper_store = PaperTradingStore(config.PAPER_TRADING_DB_PATH)
    strategies = ["tomorrow_picks"]
    payload = {"ok": True, "snapshot": [], "update": [], "paper_trade": [], "factor_ic": {}, "calibrate_live": {}}

    if args.snapshot:
        payload["snapshot"] = run_snapshots(provider, store, strategies, market=args.market)
    if args.update:
        for strategy in strategies:
            result = store.update_outcomes(provider, strategy_name=strategy)
            payload["update"].append({"strategy": strategy, "result": result})
        args.factor_ic = True
    if args.paper_trade:
        for strategy in strategies:
            result = paper_store.run_paper_trade(provider, store, strategy)
            payload["paper_trade"].append({"strategy": strategy, "result": result})
    if args.factor_ic:
        from .factor_ic import compute_factor_ic, save_factor_ic

        samples = []
        for strategy in strategies:
            samples.extend(store.live_weight_samples(strategy, days=120))
        factor_payload = compute_factor_ic(samples)
        save_factor_ic(factor_payload)
        payload["factor_ic"] = factor_payload
    if args.calibrate_live:
        from .calibrate import calibrate_live_weights

        payload["calibrate_live"] = calibrate_live_weights(
            "tomorrow_picks",
            top_k=10,
            days=120,
            steps=2,
            dry_run=not args.write_weights,
        )
        payload["tomorrow_iteration"] = _save_iteration_payload(
            payload["calibrate_live"],
            applied=payload["calibrate_live"].get("status") == "written",
        )

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
