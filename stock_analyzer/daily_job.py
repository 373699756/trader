import argparse
import json
from datetime import datetime

from . import config
from .runtime_json import atomic_write_json
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
    atomic_write_json(path, payload, ensure_ascii=False, indent=2)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="盘后每日任务：保存策略快照并回填验证结果")
    parser.add_argument("--after-close", action="store_true", help="收盘后完整流水线：下载日线、保存快照、回填、刷新因子快照和 IC")
    parser.add_argument("--download-market-data", action="store_true", help="先下载缺失或过期的本地日线历史")
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
    parser.add_argument("--market-data-codes", default="", help="传给 market_data 的逗号分隔代码；留空则全市场")
    parser.add_argument("--market-data-days", type=int, default=720, help="传给 market_data 的回溯自然日窗口")
    parser.add_argument("--market-data-limit", type=int, default=0, help="传给 market_data 的本次处理股票数上限")
    parser.add_argument("--market-data-sleep", type=float, default=0.15, help="传给 market_data 的单票下载间隔秒数")
    parser.add_argument("--market-data-force", action="store_true", help="传给 market_data，强制重抓已有数据")
    parser.add_argument("--market-data-include-st", action="store_true", help="传给 market_data，股票池包含 ST/退市名称")
    args = parser.parse_args()

    if args.after_close:
        args.download_market_data = True
        args.snapshot = True
        args.update = True
        args.factor_snapshot = True
        args.factor_ic = True

    if not any(
        (
            args.download_market_data,
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
    validation_strategies, executable_strategies = _task_strategy_sets(
        args.strategy,
        SNAPSHOT_STRATEGIES,
    )
    payload = {
        "ok": True,
        "market_data": {},
        "snapshot": [],
        "update": [],
        "paper_trade": [],
        "factor_snapshot": {},
        "factor_ic": {},
        "calibrate_live": {},
        "backup_validation": {},
    }

    if args.download_market_data:
        from .market_data import download_market_data

        try:
            payload["market_data"] = download_market_data(
                codes=_parse_code_list(args.market_data_codes),
                days=max(1, int(args.market_data_days or 720)),
                limit=max(0, int(args.market_data_limit or 0)),
                force=bool(args.market_data_force),
                include_st=bool(args.market_data_include_st),
                sleep_seconds=max(0.0, float(args.market_data_sleep or 0.0)),
            )
        except Exception as exc:
            payload["ok"] = False
            payload["market_data"] = {"ok": False, "error": str(exc)}
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 1
        if not _market_data_has_usable_rows(payload["market_data"]):
            payload["ok"] = False
            payload["error"] = "market_data_unavailable"
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 1
    if args.snapshot:
        payload["snapshot"] = run_snapshots(provider, store, validation_strategies, market=args.market)
        args.backup_validation = True
    if args.update:
        for strategy in validation_strategies:
            result = store.update_outcomes(provider, strategy_name=strategy)
            payload["update"].append({"strategy": strategy, "result": result})
        args.factor_snapshot = True
        args.factor_ic = True
    if args.paper_trade:
        from .paper_trading import PaperTradingStore

        paper_store = PaperTradingStore(config.PAPER_TRADING_DB_PATH)
        for strategy in executable_strategies:
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
        for strategy in executable_strategies:
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


def _task_strategy_sets(raw: str, supported) -> tuple:
    text = str(raw or "all").strip()
    if not text or text.lower() == "all":
        validation = [item for item in config.AUTO_SNAPSHOT_STRATEGIES if item in supported]
        executable = [item for item in config.ACTIVE_STRATEGIES if item in supported]
        return validation, executable
    selected = _parse_strategies(text, supported)
    return selected, selected


def _parse_code_list(raw: str) -> list:
    return [item.strip() for item in str(raw or "").replace("，", ",").split(",") if item.strip()]


def _market_data_has_usable_rows(result) -> bool:
    if not isinstance(result, dict):
        return False
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    return (
        _as_int(result.get("downloaded")) > 0
        or _as_int(result.get("skipped")) > 0
        or _as_int(summary.get("bar_count")) > 0
    )


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
