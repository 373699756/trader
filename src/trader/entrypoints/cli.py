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
from trader.download.domain.company_evidence import CompanyDatabaseRole
from trader.download.domain.company_sync import CompanySyncRoundRebuildBlocked
from trader.download.application.progress_message import ProgressField, ProgressLevel, ProgressMessage, ProgressTask
from trader.download.entrypoints.sync_progress import SyncProgress
from trader.recommendation.infra.settings.loading import LoadStrategySettings
from trader.infra.settings import LoadRuntimeSettings, RuntimeSettings
from trader.infra.signal_cancellation import ManualCancellation
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS, ScoringProfileId
from trader.recommendation.domain.scoring.profile_selection import ParseProfileSelection
from trader.training.entrypoints.research_evidence import COMMAND_SCHEMAS, AddResearchEvidenceParsers

if TYPE_CHECKING:
    from trader.training.entrypoints.training_progress import TrainingProgressPrinter
    from trader.training.entrypoints.report_output import TrainingReportOutput


def _parse_training_scope(value: str) -> tuple[ScoringProfileId, ...] | Strategy:
    if value in (Strategy.TOMORROW.value, Strategy.D25.value):
        return Strategy(value)
    return ParseProfileSelection(value)


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
        type=_parse_training_scope,
        default=SCORING_PROFILE_IDS,
        metavar="scope",
        help="tomorrow, d25, or a comma-separated profile list; omit to train everything.",
    )
    training.add_argument(
        "strategy_profiles", nargs="?", type=ParseProfileSelection, metavar="profiles",
        help="Comma-separated profiles after tomorrow or d25; defaults to all profiles for that strategy.",
    )
    training.add_argument("--json", action="store_true", help="Print complete result JSON to stdout.")
    training.add_argument("--diagnostics", type=Path, help="Write full diagnostics to a new file; never overwrite.")
    sync = subparsers.add_parser("sync", help="Prepare references, then synchronize history and company.")
    sync.add_argument("target", nargs="?", default="all", choices=("all", "company", "history"))
    sync.add_argument("action", nargs="?", choices=("fill",), help="Use sync company fill to resume annual announcement collection.")
    subparsers.add_parser(
        "training-reminder-status",
        help="查看已保存的训练到期与提醒状态。",
    )
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
    if args.command == "sync":
        if args.profile is not None:
            parser.error("sync does not accept --profile")
        if args.action == "fill" and args.target != "company":
            parser.error("annual announcement collection requires sync company fill")
        from trader.bootstrap import CreateSyncProgress

        inherited = os.environ.pop("TRADER_COMMAND_STARTED_MONOTONIC", None)
        progress = CreateSyncProgress(float(inherited) if inherited is not None else None)
        try:
            return _run_sync(args, progress)
        except KeyboardInterrupt:
            if args.target == "company":
                print("公司同步：已取消，已有断点保留。")
            else:
                progress(ProgressMessage((ProgressField("已有断点保留"),),
                                         task=ProgressTask("sync", "result", "结果汇总"), state="取消", finished=True))
            return 130
    if args.command == "train":
        if args.profile is not None:
            parser.error("train does not accept --profile")
        if args.strategy_profiles is not None and not isinstance(args.profiles, Strategy):
            parser.error("train accepts a second argument only after tomorrow or d25")
        return _run_train(args)
    profile_override = cast(ScoringProfileId | None, args.profile)
    try:
        config_path = _absolute_config_path(args.config)
        runtime = LoadRuntimeSettings(config_path)
        from trader.bootstrap import PrepareRuntimeDisk, ValidateConfiguration

        if args.command == "validate-config":
            from trader.entrypoints.configuration_report import ProjectValidatedConfiguration

            configuration = ValidateConfiguration(runtime, profile_override)
            print(json.dumps(ProjectValidatedConfiguration(configuration), ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        runtime = PrepareRuntimeDisk(runtime, choose=False)
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "failed", "failure": "configuration_invalid", "category": type(exc).__name__}, indent=2, ensure_ascii=False))
        return 2
    if args.command == "training-reminder-status":
        from trader.bootstrap import ExecuteTrainingReminderStatus
        from trader.training.entrypoints.training_reminder_projection import ProjectTrainingReminderStatus

        status = ExecuteTrainingReminderStatus(runtime)
        print(json.dumps(ProjectTrainingReminderStatus(status), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
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
    parser.error("unsupported command")


def _run_sync(args: argparse.Namespace, progress: SyncProgress) -> int:
    from trader.bootstrap import ExecuteSync, PrepareRuntimeDisk, ValidateConfiguration
    from trader.download.entrypoints.company_result_feedback import FormatCompanySyncSummary
    from trader.download.entrypoints.sync_projection import ProjectSyncResult, ProjectSyncTerminalState

    company_only = args.target == "company"
    task = ProgressTask("启动", "configuration", "配置检查")
    progress(ProgressMessage((), task=task, state="开始"))
    try:
        config_path = _absolute_config_path(args.config)
        runtime = LoadRuntimeSettings(config_path)
        runtime = PrepareRuntimeDisk(runtime, choose=True, report_line=progress.report_disk)
        ValidateConfiguration(runtime, None)
    except (OSError, RuntimeError, ValueError) as exc:
        progress(ProgressMessage((ProgressField(f"原因 {type(exc).__name__}"),), task=task, state="失败", finished=True))
        if company_only:
            print("公司同步：启动失败，运行配置或环境检查未通过。")
        else:
            print(json.dumps({"status": "failed", "failure": "configuration_invalid", "category": type(exc).__name__}, ensure_ascii=False))
        return 2
    progress.finish_owner("启动")
    progress(ProgressMessage((ProgressField("配置与运行环境就绪"),), task=task, state="完成", finished=True))
    with (
        ManualCancellation(timeout_seconds=runtime.pipeline.shutdown_timeout_seconds) as cancelled,
        progress.keep_alive(shutdown=cancelled),
    ):
        try:
            result = ExecuteSync(config_path, args.target, cancel_requested=cancelled.is_set, report=progress,
                                 backfill_announcements=args.action == "fill")
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            progress(ProgressMessage((ProgressField(f"原因 {type(exc).__name__}"),),
                                     task=ProgressTask("sync", "result", "结果汇总"), state="失败", finished=True))
            if company_only:
                print("公司同步：失败，同步任务未能正常返回，具体原因见上方诊断。")
            else:
                print(json.dumps({"status": "failed", "failure": "sync_input_invalid", "category": type(exc).__name__}))
            return 2
    _report_sync_failure(result, progress)
    terminal_state = ProjectSyncTerminalState(result)
    if company_only:
        print(FormatCompanySyncSummary(result, terminal_state))
        return result.exit_code
    progress(ProgressMessage(tuple(ProgressField(f"{task.task} {'待补齐' if task.status == 'pending' else task.status} 原因 {task.reason or '无'}") for task in result.tasks)
                             + tuple(ProgressField(f"company·{family.family.value} 错误 {family.result.failure}", ProgressLevel.ERROR)
                                     for task in result.tasks if task.company is not None
                                     for family in task.company.families if family.result.failure is not None)
                             + tuple(ProgressField(f"公司失败记录 company/company_sync.sqlite3；轮次 {task.company.round_id}")
                                     for task in result.tasks if task.company is not None
                                     and any(family.result.failure is not None for family in task.company.families))
                             + ((ProgressField(f"前置失败 {result.failure}"),) if result.failure is not None else ()),
                             task=ProgressTask("sync", "result", "结果汇总"),
                             state=terminal_state, finished=True))
    print(json.dumps(ProjectSyncResult(result), ensure_ascii=False, sort_keys=True, indent=2))
    return result.exit_code


def _report_sync_failure(result: SyncResult, progress: SyncProgress) -> None:
    if result.failure == "preparation_ReferenceDatabaseIncompatibleError":
        _sync_error(progress,
            "sync: ST资格及交易日历参考库 company_st.sqlite3 的结构不兼容，尚未进入下载。"
            "请退出使用该库的任务，删除下载目录 metadata 下的 company_st.sqlite3 及其"
            " -wal、-shm、-journal 配套文件，再重新运行原同步命令；程序会重新下载参考资料。",
        )
    if result.failure == "preparation_AkshareCalendarTimeoutError":
        _sync_error(progress,
            "sync: 交易日历准备超时，AkShare/Sina 在30秒内未完成；本轮尚未进入下载，已有断点保留，请稍后重试。",
        )
    for task in result.tasks:
        if task.task == "history" and task.reason == "history_format_requires_rebuild":
            _sync_error(progress,
                "sync history: 历史目录结构需要重建。停止使用历史库的任务后，归档配置数据目录下的 history/，"
                "其中包含 catalog.sqlite3、年度价格分区、封签及配套文件。然后重新运行 ./run.sh sync history。",
            )
        if task.task == "company" and task.reason == "CompanyDatabaseIncompatibleError":
            database_role = task.incompatible_company_database
            if database_role is CompanyDatabaseRole.REQUEST_JOURNAL:
                detail = "仅company/company_sync.sqlite3请求预算账本不兼容；保留已消费和未知请求记录，需核实处理，不作为资料删除重下范围。"
            elif database_role is not None:
                path = {
                    CompanyDatabaseRole.SOURCES: "company/company_sources.sqlite3",
                    CompanyDatabaseRole.FACTS: "company/company_published.sqlite3",
                }[database_role]
                detail = f"已确认不兼容的下载资料为{path}。正式库和来源库需要配套备份核验，请保留原件、模型、冻结记录和请求预算。"
            else:
                detail = "具体不兼容资料范围尚未核实，不执行删除或重建。"
            _sync_error(progress,
                "sync company: 公司下载存储结构不兼容，本次未启动公司资料写入。"
                + detail + "程序不会自动删除或迁移。",
            )
        if task.task == "company" and task.reason == CompanySyncRoundRebuildBlocked.category:
            _sync_error(progress, "sync company: 原请求轮次还不能结束，空库重下载暂缓。请保留请求账本和原资料备份，待可恢复或进入允许的新日期范围后重试。")
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

        with ManualCancellation(timeout_seconds=runtime.pipeline.shutdown_timeout_seconds) as cancelled:
            return RunResearchEvidence(
                args, project_root=runtime.project_root, config_dir=runtime.config_path.parent,
                strategy_config_path=runtime.strategy_config_path, sqlite_policy=sqlite_policy,
                cancel_requested=cancelled.is_set,
            )
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
    from trader.training.entrypoints.training_progress import TrainingProgressPrinter

    try:
        with TrainingReportOutput(json_output=args.json, diagnostics=args.diagnostics) as reports:
            return _execute_train(args, reports)
    except (OSError, ValueError) as exc:
        diagnostic = CaptureTrainingError(exc)
        output = TrainingProgressPrinter(command_label="train")
        output.report_notice("诊断输出", TrainingErrorSummary(diagnostic), state="失败")
        if args.json:
            print(json.dumps({"status": "failed", "failure": "training_report_output_failed",
                              "diagnostic": ProjectTrainingError(diagnostic)}, ensure_ascii=False))
        return 1


def _execute_train(args: argparse.Namespace, reports: TrainingReportOutput) -> int:
    from trader.training.application.error_diagnostics import CaptureTrainingError
    from trader.training.entrypoints.report_output import ProjectTrainingError, TrainingErrorSummary
    from trader.bootstrap import PrepareRuntimeDisk
    from trader.training.entrypoints.training_progress import ReadTrainingProgressLayout, TrainingProgressPrinter

    inherited = os.environ.pop("TRADER_COMMAND_STARTED_MONOTONIC", None)
    output = TrainingProgressPrinter(
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
            reports.emit({"status": "cancelled"})
            return 130
        output.report_notice("存储检测", "磁盘策略已确定", state="完成")
        output.report_notice("配置检查", "配置准备完成", state="完成")
        scope = cast(tuple[ScoringProfileId, ...] | Strategy, args.profiles)
        strategy = scope if isinstance(scope, Strategy) else None
        if isinstance(scope, Strategy):
            profiles = cast(tuple[ScoringProfileId, ...] | None, args.strategy_profiles) or SCORING_PROFILE_IDS
        else:
            profiles = scope
        strategy_label = strategy.value if strategy is not None else "Tomorrow、D25"
        output.report_notice(
            "训练范围",
            f"档位 {', '.join(profile.upper() for profile in profiles)}；检查 {strategy_label} 工件；"
            "仅使用本地已发布资料；缺资料请先执行 ./run.sh sync history",
            state="完成",
        )
        return _run_selected_training(runtime, profiles, strategy=strategy, output=output, reports=reports)


def _run_selected_training(
    runtime: RuntimeSettings,
    profiles: tuple[ScoringProfileId, ...],
    *,
    strategy: Strategy | None,
    output: TrainingProgressPrinter,
    reports: TrainingReportOutput,
) -> int:
    from trader.bootstrap import ExecuteSelectedTraining
    from trader.training.application.error_diagnostics import CaptureTrainingError
    from trader.training.entrypoints.report_output import ProjectTrainingError, TrainingErrorSummary

    try:
        output.report_notice("执行与收尾", "等待所选档位返回；含资源准备、训练、结果检查与关闭，具体进度见各档位阶段", state="开始")
        code = ExecuteSelectedTraining(runtime, profiles, started_at=output.started_at,
                                       terminal_columns=output.layout.columns, report_output=reports, strategy=strategy)
        output.report_notice("执行与收尾", f"退出码 {code}", state="取消" if code == 130 else "失败" if code else "完成")
        return code
    except KeyboardInterrupt:
        output.publish_cancelled()
        reports.emit({"status": "cancelled"})
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        diagnostic = CaptureTrainingError(exc)
        output.publish_result("failed", TrainingErrorSummary(diagnostic))
        reports.emit({"status": "failed", "category": type(exc).__name__,
                      "diagnostic": ProjectTrainingError(diagnostic)})
        return 1


def _shanghai_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


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
