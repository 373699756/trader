import argparse
import json

from . import config
from .factor_ic import compute_factor_ic, save_factor_ic
from .paper_trading import PaperTradingStore
from .providers import MarketDataProvider
from .snapshot import SNAPSHOT_STRATEGIES, run_snapshots
from .strategy_validation import StrategyValidationStore


def main() -> int:
    parser = argparse.ArgumentParser(description="盘后每日任务：保存策略快照并回填验证结果")
    parser.add_argument("--snapshot", action="store_true", help="保存策略当日预测快照")
    parser.add_argument("--update", action="store_true", help="回填已保存快照的未来收益")
    parser.add_argument("--paper-trade", action="store_true", help="根据最近保存快照生成/更新纸面组合交易")
    parser.add_argument("--factor-ic", action="store_true", help="基于真实验证样本刷新因子 IC 文件")
    parser.add_argument("--strategy", default="all", help="策略名或 all")
    parser.add_argument("--market", default="all", choices=("all", "main", "chinext", "star"))
    args = parser.parse_args()

    if not args.snapshot and not args.update and not args.paper_trade and not args.factor_ic:
        parser.error("至少指定 --snapshot、--update、--paper-trade 或 --factor-ic")

    provider = MarketDataProvider()
    store = StrategyValidationStore(config.VALIDATION_DB_PATH)
    paper_store = PaperTradingStore(config.PAPER_TRADING_DB_PATH)
    strategies = list(SNAPSHOT_STRATEGIES) if args.strategy == "all" else [args.strategy]
    payload = {"ok": True, "snapshot": [], "update": [], "paper_trade": [], "factor_ic": {}}

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
        samples = []
        for strategy in strategies:
            samples.extend(store.live_weight_samples(strategy, days=120))
        factor_payload = compute_factor_ic(samples)
        save_factor_ic(factor_payload)
        payload["factor_ic"] = factor_payload

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
