"""current configuration, performance, and explicit research command entrypoint."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from zoneinfo import ZoneInfo

from trader.domain.recommendation.model_scoring.profile_identity import SCORING_PROFILE_IDS, ScoringProfileId
from trader.infra.persistence.issuer_eligibility import SQLiteIssuerEligibilityRegistry
from trader.infra.settings import RuntimeSettings, load_long_watchlist, load_runtime_settings, load_strategy_settings

if TYPE_CHECKING:
    from trader.application.research.history_sync import HistorySyncConfiguration

_COMMAND_GROUPS = {
    "check": ("validate-config", "research-status", "history-automation-status", "performance-check"),
}
_TOMORROW_PRIORITY_LOWERED = False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trader-cli")
    parser.add_argument(
        "--config",
        default=os.environ.get("TRADER_CONFIG", ""),
        help="Absolute path to config/runtime.json.",
    )
    parser.add_argument(
        "--profile",
        choices=SCORING_PROFILE_IDS,
        help="Effective Tomorrow scoring profile for this process; config value is used when omitted.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "check",
        help="Run config validation, research readiness, and the active-profile performance gate.",
    )
    subparsers.add_parser("train-tomorrow", help="Run the due immutable Tomorrow training stage.")
    subparsers.add_parser("validate-config", help="Validate runtime and strategy configuration.")
    performance = subparsers.add_parser(
        "performance-check",
        help="Run the offline active-production performance gate without supplier network access.",
    )
    performance.add_argument("--output", type=Path)
    performance.add_argument("--baseline", type=Path)
    subparsers.add_parser(
        "download_history",
        help="Run the zero-argument historical-data maintenance workflow.",
    )
    subparsers.add_parser(
        "scheduled-history-maintenance",
        help="Run the installed zero-argument history task without environment installation.",
    )
    subparsers.add_parser(
        "history-automation-status",
        help="Read the persisted history due and reminder state without recalculation.",
    )
    subparsers.add_parser(
        "install-history-automation",
        help="Install the current user's 15:10 and 20:30 history synchronization tasks.",
    )
    subparsers.add_parser(
        "uninstall-history-automation",
        help="Remove the current user's Trader history synchronization tasks.",
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


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911 - explicit CLI command dispatch
    parser = build_parser()
    args = parser.parse_args(argv)
    maintenance_exit = _run_history_maintenance_command(
        args.command,
        cast(str, args.config),
        cast(str | None, args.profile),
        parser,
    )
    if maintenance_exit is not None:
        return maintenance_exit
    if args.command == "train-tomorrow":
        if args.profile is not None:
            parser.error("train-tomorrow does not accept --profile")
        _configure_tomorrow_training_resources()
    config_path = _absolute_config_path(args.config)
    runtime = load_runtime_settings(config_path)
    profile_override = cast(ScoringProfileId | None, args.profile)
    if args.command in _COMMAND_GROUPS:
        return _run_command_group(
            args.command,
            config_path,
            _effective_profile(runtime, profile_override),
            workers=int(getattr(args, "workers", 5)),
        )
    if args.command == "performance-check":
        return _run_performance_report(
            config_path,
            output=args.output,
            baseline=args.baseline,
            scoring_profile=profile_override,
        )
    if args.command == "research-scoring-hot-path-baseline":
        from trader.entrypoints.performance import run as run_performance

        report = run_performance(config_path, scoring_profile=profile_override)
        baseline = report["hot_path_baseline"]
        print(json.dumps(baseline, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if isinstance(baseline, dict) and baseline.get("status") == "passed" else 1
    if args.command == "eligibility-list":
        return _run_eligibility_list(runtime, as_of=args.as_of)
    if args.command == "train-tomorrow" or args.command.startswith("research-"):
        from trader.entrypoints.research_commands import ResearchCommandOptions, run_research_command

        return run_research_command(
            args.command,
            config_path,
            runtime,
            ResearchCommandOptions(
                workers=int(getattr(args, "workers", 5)),
            ),
        )
    return _run_config_validation(runtime, profile_override)


def _configure_tomorrow_training_resources() -> None:
    from trader.application.research.tomorrow_training import TOMORROW_TRAINING_COMPUTE_THREADS

    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = str(TOMORROW_TRAINING_COMPUTE_THREADS)
    _lower_tomorrow_training_priority()


def _lower_tomorrow_training_priority() -> None:
    global _TOMORROW_PRIORITY_LOWERED  # noqa: PLW0603 - one process-level resource policy
    if _TOMORROW_PRIORITY_LOWERED:
        return
    if os.name == "posix":
        os.nice(10)
        _TOMORROW_PRIORITY_LOWERED = True
        return
    if os.name != "nt":
        _TOMORROW_PRIORITY_LOWERED = True
        return
    below_normal_priority_class = 0x00004000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    if not kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), below_normal_priority_class):
        raise OSError(ctypes.__dict__["get_last_error"](), "could not lower Tomorrow training priority")
    _TOMORROW_PRIORITY_LOWERED = True


def _run_history_maintenance_command(
    command: str,
    raw_config_path: str,
    profile: str | None,
    parser: argparse.ArgumentParser,
) -> int | None:
    if command == "download_history":
        if profile is not None:
            parser.error("download_history does not accept --profile")
        return _run_history_download()
    if command == "history-automation-status":
        from trader.entrypoints.history_automation_projection import project_history_automation_status
        from trader.infra.research.history_automation_status import read_history_automation_status

        repository_root = _repository_root_for_validation()
        status = read_history_automation_status(repository_root / "data" / "history" / "baostock", _shanghai_now())
        print(json.dumps(project_history_automation_status(status), ensure_ascii=False, sort_keys=True))
        return 0
    if command not in {
        "scheduled-history-maintenance",
        "install-history-automation",
        "uninstall-history-automation",
    }:
        return None
    if profile is not None:
        parser.error(f"{command} does not accept --profile")
    config_path = _absolute_config_path(raw_config_path)
    runtime = load_runtime_settings(config_path)
    if command == "scheduled-history-maintenance":
        return _run_scheduled_history_maintenance(runtime.runtime_dir)
    return _change_history_automation_installation(command, config_path)


def _run_history_download() -> int:
    from trader.entrypoints.history_maintenance_projection import project_history_maintenance_status
    from trader.entrypoints.history_sync_progress import StderrHistorySyncProgress
    from trader.infra.research.baostock_sync_supplier import BaoStockHistorySupplier
    from trader.infra.research.history_archive_sync import run_history_sync

    repository_root = _repository_root_for_validation()
    configuration = _history_sync_configuration(repository_root)
    progress = StderrHistorySyncProgress()
    with BaoStockHistorySupplier(configuration, progress=progress) as supplier:
        status = run_history_sync(configuration, supplier, progress=progress)
    progress.publish_result(status)
    print(json.dumps(project_history_maintenance_status(status), ensure_ascii=False, sort_keys=True))
    return 0 if status.state in {"completed", "already_current"} else 1


def _run_scheduled_history_maintenance(runtime_dir: Path) -> int:
    from trader.entrypoints.history_automation_projection import project_history_automation_run_status
    from trader.infra.research.baostock_sync_supplier import BaoStockHistorySupplier
    from trader.infra.research.history_archive_sync import run_history_sync
    from trader.infra.research.history_maintenance_runner import (
        PlatformHistoryDesktopNotifier,
        RotatingHistoryAutomationLog,
        run_scheduled_history_maintenance,
    )

    observed_at = _shanghai_now()
    repository_root = _repository_root_for_validation()
    configuration = _history_sync_configuration(repository_root)
    task_log = RotatingHistoryAutomationLog(runtime_dir / "logs" / "history-automation.log")
    try:
        with BaoStockHistorySupplier(configuration, progress=task_log) as supplier:
            status = run_scheduled_history_maintenance(
                configuration,
                lambda progress: run_history_sync(
                    configuration,
                    supplier,
                    clock=lambda: observed_at,
                    progress=progress,
                ),
                PlatformHistoryDesktopNotifier(),
                observed_at,
                progress=task_log,
            )
        try:
            task_log.publish_run(status)
        except OSError:
            print(
                '{"reason":"log_write_failed","schema_version":"history_automation_log","state":"degraded"}',
                file=sys.stderr,
                flush=True,
            )
        print(json.dumps(project_history_automation_run_status(status), ensure_ascii=False, sort_keys=True))
        return 0 if status.successful else 1
    finally:
        task_log.close()


def _change_history_automation_installation(command: str, config_path: Path) -> int:
    from trader.entrypoints.history_automation_projection import project_history_automation_installation_result
    from trader.infra.research.history_automation_installation import (
        HistoryAutomationInstallationRequest,
        apply_history_automation_installation,
        plan_history_automation_installation,
        remove_history_automation_installation,
    )

    request = HistoryAutomationInstallationRequest(
        _history_automation_platform(),
        _repository_root_for_validation(),
        config_path,
        Path(sys.executable).resolve(),
        Path.home().resolve(),
        os.getuid() if hasattr(os, "getuid") else 0,
    )
    plan = plan_history_automation_installation(request)
    action = "安装" if command == "install-history-automation" else "卸载"
    print(f"将{action}以下当前用户任务文件：")
    for managed in plan.files:
        print(f"  {managed.path}")
    print("将执行：")
    commands = plan.install_commands if command == "install-history-automation" else plan.uninstall_commands
    for scheduler_command in commands:
        print("  " + " ".join(scheduler_command))
    confirmed = input(f"确认{action}？[y/N] ").strip().lower() in {"y", "yes"}
    if command == "install-history-automation":
        result = apply_history_automation_installation(plan, confirmed=confirmed)
    else:
        result = remove_history_automation_installation(plan, confirmed=confirmed)
    print(json.dumps(project_history_automation_installation_result(result), ensure_ascii=False, sort_keys=True))
    return 0


def _history_sync_configuration(repository_root: Path) -> HistorySyncConfiguration:
    from trader.application.research.history_sync import HistorySyncConfiguration

    return HistorySyncConfiguration(
        archive_root=repository_root / "data" / "history" / "baostock",
        training_root=repository_root / "data" / "train",
    )


def _history_automation_platform() -> Literal["linux", "windows", "macos"]:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    raise SystemExit("当前平台不支持历史自动化任务")


def _shanghai_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _run_config_validation(
    runtime: RuntimeSettings,
    profile_override: ScoringProfileId | None,
) -> int:
    strategy = load_strategy_settings(
        runtime.strategy_config_path,
        scoring_profile=profile_override,
    )
    watchlist = load_long_watchlist(runtime.long_watchlist_path)
    print(
        json.dumps(
            {
                "status": "ok",
                "runtime_version": runtime.config_version,
                "strategy_version": strategy.strategy_version,
                "scoring_profile": strategy.scoring_profile,
                "watchlist_version": watchlist.watchlist_version,
                "runtime_dir": str(runtime.runtime_dir),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _run_eligibility_list(runtime: RuntimeSettings, *, as_of: str | None) -> int:
    observed_at = datetime.fromisoformat(as_of) if as_of else datetime.now(ZoneInfo("Asia/Shanghai"))
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise SystemExit("--as-of must include a timezone offset")
    observed_at = observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
    registry = SQLiteIssuerEligibilityRegistry(
        runtime.runtime_dir / "issuer-eligibility.sqlite3",
        read_only=True,
    )
    facts = tuple(fact for fact in registry.facts() if fact.effective_at <= observed_at)
    status = registry.status()
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
            sort_keys=True,
        )
    )
    return 0 if status.integrity_ok else 1


def _effective_profile(
    runtime: RuntimeSettings,
    profile_override: ScoringProfileId | None,
) -> ScoringProfileId:
    if profile_override is not None:
        return profile_override
    return load_strategy_settings(runtime.strategy_config_path).scoring_profile


def _run_performance_report(
    config_path: Path,
    *,
    output: Path | None,
    baseline: Path | None,
    scoring_profile: ScoringProfileId | None,
) -> int:
    from trader.entrypoints.performance import run as run_performance

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


def _run_command_group(
    command: str,
    config_path: Path,
    profile: ScoringProfileId,
    *,
    workers: int,
) -> int:
    results: list[dict[str, str | int]] = []
    stages = _COMMAND_GROUPS[command]
    for index, stage in enumerate(stages, start=1):
        print(f"[{index}/{len(stages)}] {stage}", file=sys.stderr, flush=True)
        stage_argv = ["--config", str(config_path), "--profile", profile, stage]
        exit_code = _run_group_stage(stage_argv)
        results.append({"command": stage, "exit_code": exit_code})
    failed = any(int(item["exit_code"]) != 0 for item in results)
    print(
        json.dumps(
            {
                "schema_version": "trader_command_group",
                "command": command,
                "profile": profile,
                "status": "completed_with_failures" if failed else "passed",
                "stages": results,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 1 if failed else 0


def _run_group_stage(argv: list[str]) -> int:
    return main(argv)


def _absolute_config_path(raw_path: str) -> Path:
    if not raw_path:
        raise SystemExit("--config or TRADER_CONFIG is required")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise SystemExit("configuration path must be absolute")
    return path.resolve()


def _repository_root_for_validation() -> Path:
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "src" / "trader").is_dir():
            return candidate
    return Path("/__trader_source_not_found__")


if __name__ == "__main__":
    raise SystemExit(main())
