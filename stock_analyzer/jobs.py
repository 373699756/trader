from __future__ import annotations

import argparse
import json
import os
import sqlite3
import socket
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, List

from . import config
from .deepseek.feature_service import DeepSeekFeatureAnalysisService
from .deepseek.budget import phase_at, strategies_for_phase
from .deepseek.meta_training import DeepSeekMetaTrainingService
from .portfolio_baseline import DailyPortfolioBaselineService
from .providers import MarketDataProvider
from .snapshot import SNAPSHOT_STRATEGIES, build_deepseek_precompute_rows, run_snapshots
from .strategy_validation import StrategyValidationStore
from .validation_audit_cli import build_validation_readiness_report
from .validation_backup import backup_validation_db, list_validation_backups
from .validation_runtime_support import run_validation_tuning_once


def _snapshot_strategies(raw: str) -> List[str]:
    requested = str(raw or "").replace("，", ",").split(",")
    normalized = [item.strip() for item in requested if item.strip()]
    if not normalized or any(item.lower() == "all" for item in normalized):
        return [str(item) for item in config.AUTO_SNAPSHOT_STRATEGIES]
    chosen = [item for item in normalized if item in SNAPSHOT_STRATEGIES]
    if not chosen:
        return [str(item) for item in config.AUTO_SNAPSHOT_STRATEGIES or ()]
    return chosen


def _deepseek_strategies(raw: str, *, emergency: bool = False) -> List[str]:
    allowed = ("today_term", "tomorrow_picks", "swing_picks", "long_term_watch")
    requested = [item.strip() for item in str(raw or "").replace("，", ",").split(",") if item.strip()]
    if not requested or any(item.lower() == "all" for item in requested):
        selected = list(allowed)
    else:
        selected = [item for item in requested if item in allowed]
    phase_allowed = set(strategies_for_phase(phase_at(datetime.now(), emergency=emergency)))
    return [item for item in selected if item in phase_allowed]


def _execution_strategies(raw: str) -> List[str]:
    requested = str(raw or "").replace("，", ",").split(",")
    normalized = [item.strip() for item in requested if item.strip()]
    if not normalized or any(item.lower() == "all" for item in normalized):
        return [str(item) for item in config.ACTIVE_STRATEGIES]
    return [item for item in normalized if item in config.ACTIVE_STRATEGIES]


def _job_lock_db_path() -> str:
    base_dir = os.path.dirname(config.VALIDATION_DB_PATH)
    if not base_dir:
        base_dir = "."
    os.makedirs(base_dir, exist_ok=True)
    return os.path.join(base_dir, ".stock_analyzer_jobs.sqlite3")


def _job_owner() -> str:
    return "{}:{}:{}".format(socket.gethostname(), os.getpid(), int(time.time()))


@contextmanager
def _job_lock(command: str, lease_seconds: int = 3600):
    db_path = _job_lock_db_path()
    acquired = False
    owner = _job_owner()
    start_time = time.time()
    conn = sqlite3.connect(db_path, timeout=2)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_lease (
                command TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                acquired_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )
            """
        )
        while True:
            try:
                conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                lock_wait_seconds = time.time() - start_time
                if lock_wait_seconds > 15:
                    raise RuntimeError("job lock wait timeout for {}".format(command)) from exc
                time.sleep(min(1.0, max(0.2, lock_wait_seconds / 10)))
        now = time.time()
        existing = conn.execute(
            "SELECT owner, expires_at FROM job_lease WHERE command = ?",
            (command,),
        ).fetchone()
        if existing:
            existing_owner, expires_at = existing
            if float(expires_at) > now:
                conn.rollback()
                raise RuntimeError(
                    "job {} is locked by {} (expires at {})".format(
                        command,
                        existing_owner,
                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(expires_at))),
                    )
                )
        conn.execute(
            "INSERT OR REPLACE INTO job_lease (command, owner, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
            (command, owner, now, now + max(1, int(lease_seconds))),
        )
        # The committed lease row owns the command lock.  Holding BEGIN
        # IMMEDIATE for the job duration would block every other command and
        # keep this lease invisible to readers.
        conn.commit()
        acquired = True
        try:
            yield {
                "lock_wait_seconds": round(time.time() - start_time, 3),
                "acquired_at": now,
                "lease_seconds": max(1, int(lease_seconds)),
            }
        finally:
            conn.execute(
                "DELETE FROM job_lease WHERE command = ? AND owner = ?",
                (command, owner),
            )
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _build_payload(command: str, result: Dict[str, Any] = None, ok: bool = True, error: str = "") -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "ok": bool(ok),
        "command": command,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
    }
    if result is not None:
        payload.update(result)
    if error:
        payload["error"] = error
    return payload


def _job_metrics_path() -> str:
    base_dir = os.path.dirname(config.VALIDATION_DB_PATH)
    if not base_dir:
        base_dir = "."
    return os.path.join(base_dir, ".jobs_metrics.json")


def _load_job_metrics() -> Dict[str, object]:
    path = _job_metrics_path()
    if not os.path.exists(path):
        return {"last_run": {}, "commands": {}}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict):
            payload.setdefault("last_run", {})
            payload.setdefault("commands", {})
            return payload
    except Exception:
        return {"last_run": {}, "commands": {}}
    return {"last_run": {}, "commands": {}}


def _save_job_metrics(payload: Dict[str, object]) -> None:
    path = _job_metrics_path()
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
    except Exception:
        return


def _record_job_metrics(command: str, ok: bool, run_metrics: Dict[str, object]) -> Dict[str, object]:
    payload = _load_job_metrics()
    commands = payload.setdefault("commands", {})
    stats = commands.setdefault(
        str(command),
        {
            "attempt_count": 0,
            "success_count": 0,
            "failure_count": 0,
            "last_success_at": "",
            "last_failure_at": "",
            "last_error": "",
            "last_run_elapsed_seconds": 0.0,
        },
    )
    stats["attempt_count"] = int(stats.get("attempt_count") or 0) + 1
    stats["last_run_elapsed_seconds"] = float(run_metrics.get("elapsed_seconds") or 0.0)
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    if ok:
        stats["success_count"] = int(stats.get("success_count") or 0) + 1
        stats["last_success_at"] = now
    else:
        stats["failure_count"] = int(stats.get("failure_count") or 0) + 1
        stats["last_failure_at"] = now
        if isinstance(run_metrics.get("error"), str):
            stats["last_error"] = run_metrics["error"]
    payload["last_run"] = {
        "command": str(command),
        "started_at": str(run_metrics.get("generated_at") or ""),
        "finished_at": str(run_metrics.get("generated_at") or now),
        "ok": bool(ok),
        "lock_wait_seconds": float(run_metrics.get("lock_wait_seconds") or 0.0),
        "elapsed_seconds": float(run_metrics.get("elapsed_seconds") or 0.0),
    }
    payload["commands"] = commands
    _save_job_metrics(payload)
    return payload


def _run_snapshot_command(args) -> Dict[str, Any]:
    store = StrategyValidationStore(config.VALIDATION_DB_PATH)
    provider = MarketDataProvider()
    strategies = _snapshot_strategies(args.strategy)
    snapshots = run_snapshots(provider, store, strategies, market=str(args.market or "all"))
    return {
        "ok": all(item.get("ok") for item in snapshots),
        "strategy_count": len(strategies),
        "market": str(args.market or "all"),
        "snapshots": snapshots,
    }


def _run_deepseek_precompute_command(args) -> Dict[str, Any]:
    store = StrategyValidationStore(config.VALIDATION_DB_PATH)
    provider = MarketDataProvider()
    strategies = _deepseek_strategies(args.strategy, emergency=bool(args.emergency))
    if not strategies:
        return {
            "ok": True,
            "strategies": [],
            "status": "deadline_skipped",
            "error": "No DeepSeek strategy is eligible in the current execution window.",
            "results": [],
        }
    context = build_deepseek_precompute_rows(
        provider,
        strategies,
        market=str(args.market or "all"),
    )
    if not context.get("ok"):
        return {
            "ok": False,
            "strategies": strategies,
            "error": str(context.get("error") or "candidate_pool_unavailable"),
            "results": [],
        }
    cutoff_at = str(args.cutoff_at or context.get("cutoff_at") or "")
    service = DeepSeekFeatureAnalysisService()
    results = []
    for strategy in strategies:
        result = service.analyze(
            strategy,
            (context.get("rows_by_strategy") or {}).get(strategy, []),
            store,
            cutoff_at=cutoff_at,
            snapshot_id=str(context.get("snapshot_id") or ""),
            market_filter=str(args.market or "all"),
            deadline_at=str(args.deadline_at or getattr(config, "DEEPSEEK_PRECOMPUTE_DEADLINE", "14:48")),
            model_tier=str(args.model_tier or "flash"),
            emergency=bool(args.emergency),
        )
        results.append(result)
    successful = {
        "ok",
        "partial",
        "cache_hit",
        "no_evidence",
        "disabled",
        "daily_call_limit",
        "deadline_skipped",
    }
    return {
        "ok": all(str(item.get("status") or "") in successful for item in results),
        "strategies": strategies,
        "market": str(args.market or "all"),
        "cutoff_at": cutoff_at,
        "snapshot_id": str(context.get("snapshot_id") or ""),
        "results": results,
    }


def _run_deepseek_meta_build_command(args) -> Dict[str, Any]:
    store = StrategyValidationStore(config.VALIDATION_DB_PATH)
    provider = MarketDataProvider()
    strategies = _snapshot_strategies(args.strategy)
    service = DeepSeekMetaTrainingService(store, provider=provider)
    results = [
        service.build(
            strategy,
            min_train_days=max(5, int(args.min_train_days or 60)),
            top_k=max(1, int(args.top_k or 5)),
            bootstrap_repeats=max(100, int(args.bootstrap_repeats or 1000)),
        )
        for strategy in strategies
    ]
    return {
        "ok": all(item.get("ok") for item in results),
        "strategies": strategies,
        "results": results,
        "production_applied": False,
    }


def _run_outcome_update_command(args) -> Dict[str, Any]:
    store = StrategyValidationStore(config.VALIDATION_DB_PATH)
    provider = MarketDataProvider()
    strategies = _snapshot_strategies(args.strategy)
    updates = []
    summary = {
        "requested": 0,
        "updated": 0,
        "pending": 0,
        "unknown": 0,
        "skipped": 0,
        "execution_skipped": 0,
        "error_count": 0,
    }
    for strategy in strategies:
        item = store.update_outcomes(
            provider,
            signal_date=str(args.signal_date or ""),
            strategy_name=strategy,
            only_incomplete=bool(args.only_incomplete),
        )
        updates.append({"strategy": strategy, "result": item})
        for key in summary:
            if isinstance(item, dict):
                summary[key] += int(item.get(key) or 0)
        if not isinstance(item, dict) or item.get("ok") is False:
            summary["error_count"] += 1
    return {
        "ok": summary["error_count"] == 0,
        "signal_date": str(args.signal_date or ""),
        "strategies": strategies,
        "summary": summary,
        "updates": updates,
    }


def _run_build_portfolios_command(args) -> Dict[str, Any]:
    store = StrategyValidationStore(config.VALIDATION_DB_PATH)
    provider = MarketDataProvider()
    service = DailyPortfolioBaselineService(store)
    strategies = _execution_strategies(args.strategy)
    results = []
    for strategy in strategies:
        run_result = service.run(
            provider,
            strategy,
            signal_date=str(args.signal_date or ""),
            days=max(1, int(args.days or 120)),
            ranking_field=str(args.ranking_field or "score"),
            model_id=str(args.model_id or ""),
            top_k=max(0, int(args.top_k or 0)),
            random_seed=(None if args.random_seed is None else int(args.random_seed)),
            random_repeats=max(0, int(args.random_repeats or 0)),
        )
        results.append({"strategy": strategy, "result": run_result})
    return {
        "ok": all(item["result"].get("ok", False) for item in results),
        "signal_date": str(args.signal_date or ""),
        "strategies": strategies,
        "days": int(args.days or 120),
        "results": results,
    }


def _run_validate_command(args) -> Dict[str, Any]:
    readiness = build_validation_readiness_report(config.VALIDATION_DB_PATH)
    return {
        "ok": bool(readiness.get("ok", False)),
        "readiness": readiness,
    }


def _run_backup_command(args) -> Dict[str, Any]:
    return backup_validation_db(
        config.VALIDATION_DB_PATH,
        config.VALIDATION_BACKUP_PATH,
        label=str(args.label or "manual"),
        keep=int(args.keep or 1),
    )


def _run_tune_command(args) -> Dict[str, Any]:
    store = StrategyValidationStore(config.VALIDATION_DB_PATH)
    strategies = _snapshot_strategies(args.strategy)
    result = run_validation_tuning_once(
        store,
        cached_metrics=store.metrics,
        strategies=strategies,
        days=max(1, int(args.days or 20)),
    )
    return {
        "ok": bool(result.get("ok", False)),
        "strategies": strategies,
        "tuning": result,
    }


def _db_path_stats(db_path: str) -> Dict[str, object]:
    path = os.path.abspath(db_path)
    if not os.path.exists(path):
        return {"path": path, "size": 0, "exists": False, "wal": "", "wal_size": 0}
    wal_path = "{}-wal".format(path)
    return {
        "path": path,
        "size": int(os.path.getsize(path)),
        "exists": True,
        "wal": wal_path,
        "wal_size": int(os.path.getsize(wal_path)) if os.path.exists(wal_path) else 0,
    }


def _run_stats_command(args) -> Dict[str, object]:
    readiness = build_validation_readiness_report(config.VALIDATION_DB_PATH)
    store = StrategyValidationStore(config.VALIDATION_DB_PATH)
    repository = store.repository
    backup_list = list_validation_backups(config.VALIDATION_BACKUP_PATH)
    migrations = repository.applied_migrations()
    return {
        "ok": bool(readiness.get("ok", False)),
        "readiness": readiness,
        "db": _db_path_stats(config.VALIDATION_DB_PATH),
        "migrations": {
            "count": len(migrations),
            "applied": migrations,
        },
        "backups": {
            "count": len(backup_list),
            "latest": backup_list[0] if backup_list else {},
        },
        "metrics_file": _job_metrics_path(),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="策略后台任务入口")
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=3600,
        help="作业租约秒数（用于并发保护）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot", help="运行策略快照")
    snapshot.set_defaults(handler=_run_snapshot_command)
    snapshot.add_argument(
        "--strategy",
        default="all",
        help="逗号分隔策略名，all 表示全部快照策略",
    )
    snapshot.add_argument(
        "--market",
        default="all",
        choices=("all", "main", "chinext", "star"),
        help="快照市场过滤",
    )

    deepseek_precompute = subparsers.add_parser(
        "deepseek-precompute",
        help="异步预计算DeepSeek点时证据特征；14:30后可按需调用至生产截止时间",
    )
    deepseek_precompute.set_defaults(handler=_run_deepseek_precompute_command)
    deepseek_precompute.add_argument("--strategy", default="all", help="逗号分隔策略名")
    deepseek_precompute.add_argument(
        "--market",
        default="all",
        choices=("all", "main", "chinext", "star"),
        help="市场过滤",
    )
    deepseek_precompute.add_argument("--cutoff-at", default="", help="证据截止时间ISO；默认当前候选快照时间")
    deepseek_precompute.add_argument("--deadline-at", default="", help="API硬截止时间；默认14:48")
    deepseek_precompute.add_argument("--model-tier", default="flash", choices=("flash", "pro"))
    deepseek_precompute.add_argument(
        "--emergency",
        action="store_true",
        help="仅用于监管、减持、公告、业绩或政策突发，使用每日5次预留额度",
    )

    deepseek_meta = subparsers.add_parser(
        "deepseek-meta-build",
        help="构建三策略DeepSeek影子Meta模型与同池反事实",
    )
    deepseek_meta.set_defaults(handler=_run_deepseek_meta_build_command)
    deepseek_meta.add_argument("--strategy", default="all", help="逗号分隔策略名")
    deepseek_meta.add_argument("--min-train-days", type=int, default=60, help="扩窗训练最少真实交易日")
    deepseek_meta.add_argument("--top-k", type=int, default=5, help="同池反事实Top K")
    deepseek_meta.add_argument("--bootstrap-repeats", type=int, default=1000, help="移动块自助次数")

    outcomes = subparsers.add_parser("update-outcomes", help="执行 outcome 回填")
    outcomes.set_defaults(handler=_run_outcome_update_command)
    outcomes.add_argument(
        "--strategy",
        default="all",
        help="逗号分隔策略名，all 表示三个验证策略",
    )
    outcomes.add_argument("--signal-date", default="", help="指定信号日，不传则更新可用批次")
    outcomes.add_argument(
        "--only-incomplete",
        action="store_true",
        help="仅补齐未完成 outcome",
    )

    portfolios = subparsers.add_parser("build-portfolios", help="生成组合账本")
    portfolios.set_defaults(handler=_run_build_portfolios_command)
    portfolios.add_argument(
        "--strategy",
        default="all",
        help="逗号分隔策略名，all 表示全部可执行策略",
    )
    portfolios.add_argument("--signal-date", default="", help="指定信号日，不传则按历史窗口回填")
    portfolios.add_argument("--days", type=int, default=120, help="回放天数")
    portfolios.add_argument("--ranking-field", default="score", help="对比排名字段")
    portfolios.add_argument("--model-id", default="", help="模型标识")
    portfolios.add_argument("--top-k", type=int, default=0, help="Top K")
    portfolios.add_argument("--random-seed", type=int, default=None, help="随机种子")
    portfolios.add_argument("--random-repeats", type=int, default=0, help="随机抽样复用次数")

    validate = subparsers.add_parser("validate", help="策略数据可观测性与就绪度统计")
    validate.set_defaults(handler=_run_validate_command)

    backup = subparsers.add_parser("backup", help="执行数据库备份（支持压缩与去重）")
    backup.set_defaults(handler=_run_backup_command)
    backup.add_argument("--label", default="manual", help="备份标签")
    backup.add_argument("--keep", type=int, default=1, help="归档保留天数/个数策略的简化参数")

    tune = subparsers.add_parser("tune", help="执行策略调参与回放结果持久化")
    tune.set_defaults(handler=_run_tune_command)
    tune.add_argument(
        "--strategy",
        default="all",
        help="逗号分隔策略名，all 表示三个验证策略",
    )
    tune.add_argument("--days", type=int, default=20, help="调参窗口（交易日）")

    stats = subparsers.add_parser("stats", help="导出可观测性与数据库健康快照")
    stats.set_defaults(handler=_run_stats_command)
    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    payload: Dict[str, Any] = _build_payload(args.command, ok=False)
    command_started_at = time.time()
    metrics: Dict[str, Any] = {
        "elapsed_seconds": 0.0,
        "lock_wait_seconds": 0.0,
        "lock_acquired": 0,
    }
    try:
        with _job_lock(args.command, lease_seconds=max(60, int(args.lease_seconds or 3600))) as lock_info:
            result = args.handler(args)
            metrics["lock_wait_seconds"] = float((lock_info or {}).get("lock_wait_seconds") or 0.0)
            metrics["lock_acquired"] = 1
            payload = _build_payload(args.command, result=result, ok=True)
    except Exception as exc:
        payload = _build_payload(args.command, ok=False, error=str(exc))
        metrics["lock_acquired"] = 0
        metrics["elapsed_seconds"] = round(time.time() - command_started_at, 3)
        metrics["error"] = str(exc)
        metrics["generated_at"] = payload.get("generated_at")
        latest_metrics = _record_job_metrics(args.command, False, metrics)
        payload["metrics"] = dict(
            latest_metrics["commands"].get(str(args.command), {}),
            **{
                "lock_wait_seconds": float(metrics.get("lock_wait_seconds") or 0.0),
                "elapsed_seconds": float(metrics.get("elapsed_seconds") or 0.0),
                "lock_acquired": int(metrics.get("lock_acquired") or 0),
                "last_error": str(metrics.get("error") or ""),
            },
        )
        if isinstance(payload.get("result"), dict):
            payload["result"]["metrics"] = payload["metrics"]
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return 1

    metrics["elapsed_seconds"] = round(time.time() - command_started_at, 3)
    metrics["generated_at"] = payload.get("generated_at")
    metrics["error"] = payload.get("error", "")
    latest_metrics = _record_job_metrics(args.command, bool(result.get("ok", False) if isinstance(result, dict) else True), metrics)
    payload["metrics"] = dict(
        latest_metrics["commands"].get(str(args.command), {}),
        **{
            "lock_wait_seconds": float(metrics.get("lock_wait_seconds") or 0.0),
            "elapsed_seconds": float(metrics.get("elapsed_seconds") or 0.0),
            "lock_acquired": int(metrics.get("lock_acquired") or 0),
            "last_error": str(metrics.get("error") or ""),
        },
    )
    if isinstance(payload.get("result"), dict):
        payload["result"]["metrics"] = payload["metrics"]
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
