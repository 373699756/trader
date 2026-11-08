"""current configuration, performance, and explicit research command entrypoint."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import argparse
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

from trader.download.application.sync_data import SyncResult
from trader.download.application.progress_message import ProgressField, ProgressMessage, ProgressTask
from trader.download.entrypoints.sync_progress import SyncProgress
from trader.recommendation.infra.settings.loading import LoadLongWatchlist, LoadStrategySettings
from trader.infra.settings import LoadRuntimeSettings, RuntimeSettings
from trader.infra.signal_cancellation import ManualCancellation
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS, ScoringProfileId
from trader.recommendation.domain.scoring.profile_selection import ParseProfileSelection
from trader.training.entrypoints.research_evidence import COMMAND_SCHEMAS, AddResearchEvidenceParsers

if TYPE_CHECKING:
    from trader.training.entrypoints.tomorrow_training_progress import StderrTomorrowTrainingProgress
    from trader.training.entrypoints.report_output import TrainingReportOutput


class _FillFlag(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        namespace.fill = True


def BuildParser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trader-cli")
    parser.add_argument(
        "--config",
        default=os.environ.get("TRADER_CONFIG", ""),
        help="Absolute path to config/runtime.json.",
    )
    parser.add_argument(
        "--profile",
        choices=SCORING_PROFILE_IDS,
        help="Effective scoring profile for this process; config value is used when omitted.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    AddResearchEvidenceParsers(subparsers)
    subparsers.add_parser("validate-config", help="Validate runtime and strategy configuration.")
    performance = subparsers.add_parser(
        "performance-check",
        help="Run the offline active-production performance gate without supplier network access.",
    )
    performance.add_argument("--output", type=Path)
    performance.add_argument("--baseline", type=Path)
    training = subparsers.add_parser("train", help="Train all or the selected profiles using published local history.")
    training.add_argument(
        "profiles",
        nargs="?",
        type=ParseProfileSelection,
        default=SCORING_PROFILE_IDS,
        help="Comma-separated profiles; defaults to all profiles.",
    )
    training.add_argument("--json", action="store_true", help="Print complete result JSON to stdout.")
    training.add_argument("--diagnostics", type=Path, help="Write full diagnostics to a new file; never overwrite.")
    sync = subparsers.add_parser("sync", help="Prepare references, then synchronize history, market and company.")
    sync.add_argument("target", nargs="?", default="all", choices=("all", "market", "company", "history"))
    sync.add_argument("--fill", action=_FillFlag, nargs=0, default=False, help="补充所选资料的缺口：history/market 自动用 BaoStock；company 补历史及正文并自动 OCR/扩限。")
    sync.add_argument("--repair-codes", nargs="+", help="仅history/market --fill：限定本次补采的1至5个六位股票代码，保留完整发布总体。")
    subparsers.add_parser(
        "history-automation-status",
        help="Read the persisted history due and reminder state without recalculation.",
    )
    subparsers.add_parser("history-automation-uninstall", help="Explicitly remove retired Trader user scheduler tasks.")
    subparsers.add_parser("research-status", help="Read immutable research coverage and capacity status.")
    subparsers.add_parser(
        "research-scoring-hot-path-baseline",
        help="Run the read-only scoring hot-path equivalence and efficiency baseline.",
    )
    subparsers.add_parser(
        "research-baseline-audit",
        help="Audit packaged models, configuration, research conclusions, and live identity without writes.",
    )
    eligibility = subparsers.add_parser(
        "eligibility-list",
        help="Read the immutable level-one permanent issuer exclusion list without supplier requests.",
    )
    eligibility.add_argument(
        "--as-of", help="Timezone-aware ISO-8601 point-in-time; defaults to current Shanghai time."
    )
    return parser


def Main(argv: list[str] | None = None) -> int:  # noqa: PLR0911 - explicit CLI command dispatch
    parser = BuildParser()
    args = parser.parse_args(argv)
    if args.command == "sync" and args.fill and args.target == "all":
        parser.error("--fill 须指定 history、market 或 company")
    if args.command == "sync":
        if args.repair_codes is not None:
            from trader.download.domain.price_repair import ValidateRepairSelection

            try:
                if len(set(args.repair_codes)) != len(args.repair_codes):
                    raise ValueError("repair_codes_must_be_unique")
                ValidateRepairSelection(frozenset(args.repair_codes), fill=args.fill, target=args.target)
            except ValueError as exc:
                parser.error(str(exc))
        if args.profile is not None:
            parser.error("sync does not accept --profile")
        from trader.bootstrap import CreateSyncProgress

        inherited = os.environ.pop("TRADER_COMMAND_STARTED_MONOTONIC", None)
        progress = CreateSyncProgress(float(inherited) if inherited is not None else None)
        try:
            return _run_sync(args, progress)
        except KeyboardInterrupt:
            progress(ProgressMessage((ProgressField("已有断点保留"),),
                                     task=ProgressTask("sync", "result", "结果汇总"), state="取消", finished=True))
            return 130
    if args.command == "train":
        if args.profile is not None:
            parser.error("train does not accept --profile")
        return _run_train(args)
    try:
        config_path = _absolute_config_path(args.config)
        runtime = LoadRuntimeSettings(config_path)
        if args.command != "validate-config":
            from trader.bootstrap import PrepareRuntimeDisk

            runtime = PrepareRuntimeDisk(runtime, choose=args.command in ("sync", "train"))
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "failed", "failure": "configuration_invalid", "category": type(exc).__name__}, indent=2, ensure_ascii=False))
        return 2
    profile_override = cast(ScoringProfileId | None, args.profile)
    if args.command == "history-automation-status":
        from trader.bootstrap import ExecuteTrainingReminderStatus
        from trader.training.entrypoints.training_reminder_projection import ProjectTrainingReminderStatus

        status = ExecuteTrainingReminderStatus(runtime)
        print(json.dumps(ProjectTrainingReminderStatus(status), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.command == "history-automation-uninstall":
        from trader.download.entrypoints.commands import RunDownloadCommand

        return RunDownloadCommand(args.command, config_path=config_path)
    if args.command == "performance-check":
        return _run_performance_report(
            config_path,
            output=args.output,
            baseline=args.baseline,
            scoring_profile=profile_override,
        )
    if args.command == "research-scoring-hot-path-baseline":
        from trader.entrypoints.performance import Run as run_performance

        report = run_performance(config_path, scoring_profile=profile_override)
        baseline = report["hot_path_baseline"]
        print(json.dumps(baseline, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if isinstance(baseline, dict) and baseline.get("status") == "passed" else 1
    if args.command == "eligibility-list":
        from trader.bootstrap import BuildSqlitePolicy

        return _run_eligibility_list(runtime, as_of=args.as_of, sqlite_policy=BuildSqlitePolicy(runtime))
    if args.command.startswith("research-"):
        from trader.bootstrap import BuildSqlitePolicy

        return _run_training_command(args, config_path, runtime, parser, sqlite_policy=BuildSqlitePolicy(runtime))
    return _run_config_validation(runtime, profile_override)


def _run_sync(args: argparse.Namespace, progress: SyncProgress) -> int:
    from trader.bootstrap import ExecuteSync, PrepareRuntimeDisk
    from trader.download.entrypoints.sync_projection import ProjectSyncResult

    task = ProgressTask("启动", "configuration", "配置检查")
    progress(ProgressMessage((), task=task, state="开始"))
    try:
        config_path = _absolute_config_path(args.config)
        runtime = LoadRuntimeSettings(config_path)
        runtime = PrepareRuntimeDisk(runtime, choose=True, report_line=progress.report_disk)
        _configuration_payload(runtime, None)
    except (OSError, RuntimeError, ValueError) as exc:
        progress(ProgressMessage((ProgressField(f"原因 {type(exc).__name__}"),), task=task, state="失败", finished=True))
        print(json.dumps({"status": "failed", "failure": "configuration_invalid", "category": type(exc).__name__}, ensure_ascii=False))
        return 2
    progress.finish_owner("启动")
    progress(ProgressMessage((ProgressField("配置与运行环境就绪"),), task=task, state="完成", finished=True))
    with (
        ManualCancellation(timeout_seconds=runtime.pipeline.shutdown_timeout_seconds) as cancelled,
        progress.keep_alive(shutdown=cancelled),
    ):
        try:
            result = ExecuteSync(config_path, args.target, cancel_requested=cancelled.is_set,
                                 fill_missing=args.fill, report=progress,
                                 repair_codes=frozenset(args.repair_codes) if args.repair_codes is not None else None)
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            progress(ProgressMessage((ProgressField(f"原因 {type(exc).__name__}"),),
                                     task=ProgressTask("sync", "result", "结果汇总"), state="失败", finished=True))
            print(json.dumps({"status": "failed", "failure": "sync_input_invalid", "category": type(exc).__name__}))
            return 2
    _report_sync_failure(result, progress)
    summary = ProjectSyncResult(result)
    progress(ProgressMessage(tuple(ProgressField(f"{task.task} {'待补齐' if task.status == 'pending' else task.status} 原因 {task.reason or '无'}") for task in result.tasks)
                             + ((ProgressField(f"前置失败 {result.failure}"),) if result.failure is not None else ()),
                             task=ProgressTask("sync", "result", "结果汇总"),
                             state="取消" if summary["status"] == "cancelled" else "失败" if summary["status"] == "failed"
                             else "完成" if summary["status"] == "completed" else "等待", finished=True))
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return result.exit_code


def _report_sync_failure(result: SyncResult, progress: SyncProgress) -> None:
    if result.failure == "preparation_ReferenceDatabaseIncompatibleError":
        _sync_error(progress,
            "sync: ST资格及交易日历参考库 company_st.sqlite3 的结构不兼容，尚未进入下载。"
            "请退出使用该库的任务，删除下载目录 market/reference 下的 company_st.sqlite3 及其"
            " -wal、-shm、-journal 配套文件，再重新运行原同步命令；程序会重新下载参考资料。",
        )
    if result.failure == "preparation_AkshareCalendarTimeoutError":
        _sync_error(progress,
            "sync: 交易日历准备超时，AkShare/Sina 在30秒内未完成；本轮尚未进入下载，已有断点保留，请稍后重试。",
        )
    for task in result.tasks:
        if task.task == "company" and task.reason == "ProcessLockError":
            _sync_error(progress,
                "sync company: 公司同步锁正由另一个任务持有。若曾按 Ctrl+Z，请在原终端用 jobs 查看，"
                "用 fg 恢复对应任务后按 Ctrl+C 退出，等待释放锁后再续传。",
            )


def _sync_error(progress: SyncProgress, text: str) -> None:
    progress(ProgressMessage((ProgressField(text),), task=ProgressTask("sync", "failure", "失败诊断"),
                             state="失败", finished=True))


def _run_training_command(
    args: argparse.Namespace, config_path: Path, runtime: RuntimeSettings, parser: argparse.ArgumentParser, *, sqlite_policy: SqliteIoPolicy
) -> int:
    if args.command in COMMAND_SCHEMAS:
        if args.profile is not None:
            parser.error(f"{args.command} does not accept --profile")
        from trader.training.entrypoints.research_evidence import RunResearchEvidence

        return RunResearchEvidence(args, project_root=runtime.project_root, config_dir=runtime.config_path.parent, sqlite_policy=sqlite_policy)
    from trader.training.entrypoints.commands import ResearchCommandOptions, RunResearchCommand

    return RunResearchCommand(
        args.command,
        config_path,
        runtime,
        ResearchCommandOptions(
            workers=int(getattr(args, "workers", 5)), profile=cast(ScoringProfileId | None, args.profile)
        ), sqlite_policy=sqlite_policy,
    )


def _run_train(args: argparse.Namespace) -> int:
    from trader.training.application.error_diagnostics import CaptureTrainingError
    from trader.training.entrypoints.report_output import ProjectTrainingError, TrainingErrorSummary, TrainingReportOutput
    from trader.training.entrypoints.tomorrow_training_progress import StderrTomorrowTrainingProgress

    try:
        with TrainingReportOutput(json_output=args.json, diagnostics=args.diagnostics) as reports:
            return _execute_train(args, reports)
    except (OSError, ValueError) as exc:
        diagnostic = CaptureTrainingError(exc)
        output = StderrTomorrowTrainingProgress(command_label="train")
        output.report_notice("诊断输出", TrainingErrorSummary(diagnostic), state="失败")
        if args.json:
            print(json.dumps({"status": "failed", "failure": "training_report_output_failed",
                              "diagnostic": ProjectTrainingError(diagnostic)}, ensure_ascii=False))
        return 1


def _execute_train(args: argparse.Namespace, reports: TrainingReportOutput) -> int:
    from trader.training.application.error_diagnostics import CaptureTrainingError
    from trader.training.entrypoints.report_output import ProjectTrainingError, TrainingErrorSummary
    from trader.bootstrap import PrepareRuntimeDisk
    from trader.training.entrypoints.tomorrow_training_progress import ReadTrainingProgressLayout, StderrTomorrowTrainingProgress

    inherited = os.environ.pop("TRADER_COMMAND_STARTED_MONOTONIC", None)
    output = StderrTomorrowTrainingProgress(
        command_label="train", started_at=float(inherited) if inherited is not None else None,
        layout=ReadTrainingProgressLayout(),
    )
    with output:
        output.report_notice("配置检查", "正在读取运行配置及磁盘策略", state="开始")
        try:
            runtime = LoadRuntimeSettings(_absolute_config_path(args.config))
            runtime = PrepareRuntimeDisk(runtime, choose=True, report_line=output.report_disk)
        except (OSError, RuntimeError, ValueError) as exc:
            diagnostic = CaptureTrainingError(exc)
            output.report_notice("配置检查", TrainingErrorSummary(diagnostic), state="失败")
            reports.emit({"status": "failed", "failure": "configuration_invalid", "category": type(exc).__name__,
                          "diagnostic": ProjectTrainingError(diagnostic)})
            return 2
        except KeyboardInterrupt:
            output.publish_cancelled()
            reports.emit({"schema_version": "selected_training_result", "status": "cancelled"})
            return 130
        output.report_notice("存储检测", "磁盘策略已确定", state="完成")
        output.report_notice("配置检查", "配置准备完成", state="完成")
        profiles = cast(tuple[ScoringProfileId, ...], args.profiles)
        output.report_notice("训练范围", f"档位 {', '.join(profile.upper() for profile in profiles)}；仅使用本地已发布资料；缺资料请先执行 ./run.sh sync history", state="完成")
        return _run_selected_training(runtime, profiles, output=output, reports=reports)


def _run_selected_training(
    runtime: RuntimeSettings, profiles: tuple[ScoringProfileId, ...], *,
    output: StderrTomorrowTrainingProgress,
    reports: TrainingReportOutput,
) -> int:
    from trader.bootstrap import ExecuteSelectedTraining
    from trader.training.application.error_diagnostics import CaptureTrainingError
    from trader.training.entrypoints.report_output import ProjectTrainingError, TrainingErrorSummary

    try:
        output.report_notice("执行与收尾", "等待所选档位返回；含资源准备、训练、结果检查与关闭，具体进度见各档位阶段", state="开始")
        code = ExecuteSelectedTraining(runtime, profiles, started_at=output.started_at,
                                       terminal_columns=output.layout.columns, report_output=reports)
        output.report_notice("执行与收尾", f"退出码 {code}", state="取消" if code == 130 else "失败" if code else "完成")
        return code
    except KeyboardInterrupt:
        output.publish_cancelled()
        reports.emit({"schema_version": "selected_training_result", "status": "cancelled"})
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        diagnostic = CaptureTrainingError(exc)
        output.publish_result("failed", TrainingErrorSummary(diagnostic))
        reports.emit({"schema_version": "selected_training_result", "status": "failed", "category": type(exc).__name__,
                      "diagnostic": ProjectTrainingError(diagnostic)})
        return 1


def _shanghai_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _run_config_validation(runtime: RuntimeSettings, profile_override: ScoringProfileId | None) -> int:
    print(json.dumps(_configuration_payload(runtime, profile_override), ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def _configuration_payload(runtime: RuntimeSettings, profile_override: ScoringProfileId | None) -> dict[str, object]:
    strategy = LoadStrategySettings(
        runtime.strategy_config_path,
        scoring_profile=profile_override,
    )
    watchlist = LoadLongWatchlist(runtime.long_watchlist_path)
    from trader.download.infra.download_settings import LoadDownloadFailureConfiguration, LoadHistorySyncConfiguration, LoadQfqWindowConfiguration
    from trader.recommendation.infra.scoring.cost_settings import LoadExecutionCostPolicy
    from trader.training.infra.research_settings import LoadEvaluationCostScenarios
    from trader.training.infra.profile.v1.settings import LoadV1TrainingSettings
    from trader.training.infra.profile.v5.configuration import TcnConfigurationDocument
    from trader.training.infra.profile.v5.settings import TcnFittingParameters
    from trader.training.infra.research_settings import LoadResearchSettings
    from trader.training.infra.training_settings import LoadProfileTrainingSettings

    config_dir = runtime.config_path.parent
    download = LoadHistorySyncConfiguration(runtime.project_root, runtime.data_paths)
    qfq = LoadQfqWindowConfiguration(runtime.project_root)
    failure = LoadDownloadFailureConfiguration(runtime.project_root)
    training_v2 = LoadProfileTrainingSettings(runtime.project_root, "v2")
    training_v3 = LoadProfileTrainingSettings(runtime.project_root, "v3")
    training_v1 = LoadV1TrainingSettings(runtime.project_root)
    research = LoadResearchSettings(config_dir)
    cost = LoadExecutionCostPolicy(config_dir)
    cost_scenarios = LoadEvaluationCostScenarios(config_dir)
    fit_dates = {"v1": training_v1.fit_dates, "v2": training_v2.fit_dates, "v3": training_v3.fit_dates}
    training_v5 = LoadProfileTrainingSettings(runtime.project_root, "v5")
    if not isinstance(training_v5.fitting, TcnFittingParameters):
        raise ValueError("V5 training requires TCN parameters")
    fit_dates["v5"] = training_v5.fit_dates
    return {
        "status": "ok",
        "runtime_version": runtime.config_version,
        "strategy_version": strategy.strategy_version,
        "scoring_profile": strategy.scoring_profile,
        "watchlist_version": watchlist.watchlist_version,
        "runtime_dir": str(runtime.runtime_dir),
        "data_dir": str(runtime.data_paths.root),
        "shutdown_timeout_seconds": runtime.pipeline.shutdown_timeout_seconds,
        "history_sessions": download.sessions,
        "history_write_batches": {
            "maximum_codes": download.write_batches.maximum_codes,
            "maximum_cells": download.write_batches.maximum_cells,
        },
        "qfq_sessions": {"v2": qfq.v2_sessions, "v3": qfq.v3_sessions},
        "qfq_workers": qfq.workers,
        "qfq_source_timeout_seconds": qfq.source_timeout_seconds,
        "qfq_source_retries": qfq.source_retries,
        "download_failure_diagnostics": {"retention_limit": failure.retention_limit, "query_limit": failure.query_limit},
        "sqlite_policy": {
            "hdd": {
                "connection_timeout_seconds": runtime.sqlite_policy.hdd.connection_timeout_seconds,
                "read_window_rows": runtime.sqlite_policy.hdd.read_window_rows,
                "transaction_batch_rows": runtime.sqlite_policy.hdd.transaction_batch_rows,
                "checkpoint_mode": runtime.sqlite_policy.hdd.checkpoint_mode,
            },
            "ssd": {
                "connection_timeout_seconds": runtime.sqlite_policy.ssd.connection_timeout_seconds,
                "read_window_rows": runtime.sqlite_policy.ssd.read_window_rows,
                "transaction_batch_rows": runtime.sqlite_policy.ssd.transaction_batch_rows,
                "checkpoint_mode": runtime.sqlite_policy.ssd.checkpoint_mode,
            },
        },
        "training_fit_dates": fit_dates,
        "v5_training_configuration": TcnConfigurationDocument(training_v5.fit_dates, training_v5.fitting),
        "research_arms": len(research.ablation_arms),
        "terminal_holdout_sessions": research.terminal_holdout_sessions,
        "online_round_trip_base_rate": cost.round_trip_base_rate,
        "evaluation_round_trip_bps": [round(item.round_trip_return * 10_000) for item in cost_scenarios],
    }


def _run_eligibility_list(runtime: RuntimeSettings, *, as_of: str | None, sqlite_policy: SqliteIoPolicy) -> int:
    from trader.bootstrap import CreateCompanyEligibilityReader

    observed_at = datetime.fromisoformat(as_of) if as_of else datetime.now(ZoneInfo("Asia/Shanghai"))
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise SystemExit("--as-of must include a timezone offset")
    observed_at = observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
    event_policy = LoadStrategySettings(runtime.strategy_config_path).company_scoring.events
    eligibility_index = CreateCompanyEligibilityReader(runtime.data_paths.company, event_policy, sqlite_policy=sqlite_policy)
    facts = tuple(fact for fact in eligibility_index.facts() if fact.effective_at <= observed_at)
    status = eligibility_index.status()
    print(
        json.dumps(
            {
                "schema_version": "issuer_eligibility_list",
                "as_of": observed_at.isoformat(),
                "manifest_hash": status.manifest_hash,
                "integrity_ok": status.integrity_ok,
                "items": [
                    {
                        "code": fact.code,
                        "reason": fact.reason.value,
                        "effective_at": fact.effective_at.isoformat(),
                        "evidence_id": fact.evidence_id,
                        "source": fact.source,
                        "evidence_hash": fact.evidence_hash,
                    }
                    for fact in facts
                ],
            },
            ensure_ascii=False,
            sort_keys=True, indent=2,
        )
    )
    return 0 if status.integrity_ok else 1


def _run_performance_report(
    config_path: Path,
    *,
    output: Path | None,
    baseline: Path | None,
    scoring_profile: ScoringProfileId | None,
) -> int:
    from trader.entrypoints.performance import Run as run_performance

    report = run_performance(
        config_path,
        baseline_path=baseline,
        scoring_profile=scoring_profile,
    )
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if output is not None:
        output.write_text(f"{payload}\n", encoding="utf-8")
    print(payload)
    return 0 if report["status"] == "passed" else 1


def _absolute_config_path(raw_path: str) -> Path:
    if not raw_path:
        raise ValueError("--config or TRADER_CONFIG is required")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise ValueError("configuration path must be absolute")
    return path.resolve()


if __name__ == "__main__":
    raise SystemExit(Main())
