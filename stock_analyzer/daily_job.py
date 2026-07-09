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
    parser.add_argument("--factor-snapshot", action="store_true", help="基于本地 market_data 生成 Qlib 风格因子快照表")
    parser.add_argument("--factor-ic", action="store_true", help="基于真实验证样本刷新因子 IC 文件")
    parser.add_argument("--calibrate-live", action="store_true", help="基于明天预测真实验证样本校准权重")
    parser.add_argument("--write-weights", action="store_true", help="与 --calibrate-live 配合，确认写入 weights.json")
    parser.add_argument("--backup-validation", action="store_true", help="备份荐股验证数据库")
    parser.add_argument("--list-validation-backups", action="store_true", help="列出荐股验证数据库备份")
    parser.add_argument("--restore-validation", default="", help="从指定备份文件还原荐股验证数据库；还原前会自动备份当前库")
    parser.add_argument("--strategy", default="all", help="策略名；all 表示所有快照策略")
    parser.add_argument("--market", default="all", choices=("all", "main", "chinext", "star"))
    args = parser.parse_args()

    if not any(
        (
            args.snapshot,
            args.update,
            args.paper_trade,
            args.factor_snapshot,
            args.factor_ic,
            args.calibrate_live,
            args.backup_validation,
            args.list_validation_backups,
            bool(args.restore_validation),
        )
    ):
        parser.error("至少指定一个任务参数")

    from .validation_backup import backup_validation_db, list_validation_backups, restore_validation_db

    if args.list_validation_backups:
        print(json.dumps({"ok": True, "backups": list_validation_backups(config.VALIDATION_BACKUP_PATH)}, ensure_ascii=False, indent=2))
        return 0
    if args.restore_validation:
        payload = restore_validation_db(
            args.restore_validation,
            config.VALIDATION_DB_PATH,
            config.VALIDATION_BACKUP_PATH,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload.get("ok") else 1

    from .providers import MarketDataProvider
    from .snapshot import SNAPSHOT_STRATEGIES, run_snapshots
    from .strategy_validation import StrategyValidationStore

    provider = MarketDataProvider()
    store = StrategyValidationStore(config.VALIDATION_DB_PATH)
    strategies = _parse_strategies(args.strategy, SNAPSHOT_STRATEGIES)
    payload = {
        "ok": True,
        "snapshot": [],
        "update": [],
        "paper_trade": [],
        "factor_snapshot": {},
        "factor_ic": {},
        "calibrate_live": {},
        "backup_validation": {},
    }

    if args.snapshot:
        payload["snapshot"] = run_snapshots(provider, store, strategies, market=args.market)
        args.backup_validation = True
    if args.update:
        for strategy in strategies:
            result = store.update_outcomes(provider, strategy_name=strategy)
            payload["update"].append({"strategy": strategy, "result": result})
        args.factor_snapshot = True
        args.factor_ic = True
    if args.paper_trade:
        from .paper_trading import PaperTradingStore

        paper_store = PaperTradingStore(config.PAPER_TRADING_DB_PATH)
        for strategy in strategies:
            result = paper_store.run_paper_trade(provider, store, strategy)
            payload["paper_trade"].append({"strategy": strategy, "result": result})
    if args.factor_snapshot:
        from .factor_snapshot import build_factor_snapshots

        payload["factor_snapshot"] = build_factor_snapshots(
            config.MARKET_DATA_DB_PATH,
            config.FACTOR_SNAPSHOT_DB_PATH,
            days=config.FACTOR_SNAPSHOT_HISTORY_DAYS,
            batch_size=config.FACTOR_SNAPSHOT_BATCH_SIZE,
        )
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
    if args.backup_validation:
        payload["backup_validation"] = backup_validation_db(
            config.VALIDATION_DB_PATH,
            config.VALIDATION_BACKUP_PATH,
            label="daily_job",
        )

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _parse_strategies(raw: str, supported) -> list:
    text = str(raw or "all").strip()
    if not text or text.lower() == "all":
        return list(supported)
    requested = [item.strip() for item in text.replace("，", ",").split(",") if item.strip()]
    strategies = [item for item in requested if item in supported]
    return strategies or ["tomorrow_picks"]


if __name__ == "__main__":
    raise SystemExit(main())
