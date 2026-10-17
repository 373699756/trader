"""current configuration, performance, and explicit research command entrypoint."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from trader.infra.settings import LoadLongWatchlist, LoadRuntimeSettings, LoadStrategySettings, RuntimeSettings
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS, ScoringProfileId
from trader.recommendation.infra.persistence.issuer_eligibility import SQLiteIssuerEligibilityIndex
from trader.training.entrypoints.research_evidence import COMMAND_SCHEMAS, AddResearchEvidenceParsers

_COMMAND_GROUPS = {
    "check": ("validate-config", "research-status", "history-automation-status", "performance-check"),
}
_TOMORROW_PRIORITY_LOWERED = False


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
    subparsers.add_parser(
        "check",
        help="Run config validation, research readiness, and the active-profile performance gate.",
    )
    subparsers.add_parser("train-v2", help="Rebuild both V2 heads with 251-session features and overwrite artifacts.")
    subparsers.add_parser("train-v3", help="Rebuild both heads with one scan and overwrite fixed V3 artifacts.")
    subparsers.add_parser("train-both", help="Build V2/V3 samples from one history scan and fit both profiles.")
    subparsers.add_parser("validate-config", help="Validate runtime and strategy configuration.")
    performance = subparsers.add_parser(
        "performance-check",
        help="Run the offline active-production performance gate without supplier network access.",
    )
    performance.add_argument("--output", type=Path)
    performance.add_argument("--baseline", type=Path)
    subparsers.add_parser(
        "download",
        help="Run the zero-argument historical-data maintenance workflow.",
    )
    subparsers.add_parser(
        "qfq_download", help="Update Tencent V2/V3 bounded daily windows concurrently; resume automatically."
    )
    subparsers.add_parser("risk_download", help="Prepare current company research within a bounded task budget.")
    subparsers.add_parser("risk_history_download", help="Resume CNInfo issuer announcement history separately.")
    subparsers.add_parser(
        "scheduled-history-maintenance",
        help="Run the installed zero-argument history task without environment installation.",
    )
    subparsers.add_parser(
        "history-automation-status",
        help="Read the persisted history due and reminder state without recalculation.",
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
    maintenance_exit = _run_history_maintenance_command(
        args.command,
        cast(str, args.config),
        cast(str | None, args.profile),
        parser,
    )
    if maintenance_exit is not None:
        return maintenance_exit
    if args.command in {"train-v2", "train-v3", "train-both"}:
        if args.profile is not None:
            parser.error(f"{args.command} does not accept --profile")
        from trader.training.entrypoints.commands import ConfigureTrainingResources

        ConfigureTrainingResources()
    config_path = _absolute_config_path(args.config)
    runtime = LoadRuntimeSettings(config_path)
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
        from trader.entrypoints.performance import Run as run_performance

        report = run_performance(config_path, scoring_profile=profile_override)
        baseline = report["hot_path_baseline"]
        print(json.dumps(baseline, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if isinstance(baseline, dict) and baseline.get("status") == "passed" else 1
    if args.command == "eligibility-list":
        return _run_eligibility_list(runtime, as_of=args.as_of)
    if args.command in {"train-v2", "train-v3", "train-both"} or args.command.startswith("research-"):
        return _run_training_command(args, config_path, runtime, parser)
    return _run_config_validation(runtime, profile_override)


def _run_training_command(
    args: argparse.Namespace, config_path: Path, runtime: RuntimeSettings, parser: argparse.ArgumentParser
) -> int:
    if args.command in COMMAND_SCHEMAS:
        if args.profile is not None:
            parser.error(f"{args.command} does not accept --profile")
        from trader.training.entrypoints.research_evidence import RunResearchEvidence

        return RunResearchEvidence(args, repository_root=runtime.project_root)
    from trader.training.entrypoints.commands import ResearchCommandOptions, RunResearchCommand

    return RunResearchCommand(
        args.command, config_path, runtime, ResearchCommandOptions(workers=int(getattr(args, "workers", 5)))
    )


def _configure_tomorrow_training_resources() -> None:
    from trader.training.application.tomorrow_training import TOMORROW_TRAINING_COMPUTE_THREADS

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
    if command in {"risk_download", "risk_history_download"}:
        if profile is not None:
            parser.error(f"{command} does not accept --profile")
        from trader.infra.process_lock import ProcessLock, ProcessLockError
        from trader.recommendation.entrypoints.risk_download_progress import StderrRiskDownloadProgress

        try:
            settings = LoadRuntimeSettings(_absolute_config_path(raw_config_path))
            with ProcessLock(settings.runtime_dir / "risk_download.lock", label=command):
                risk_progress = StderrRiskDownloadProgress(command_name=command)
                risk_progress.start()
                try:
                    from trader.bootstrap import ExecuteRiskDownload

                    risk_result = ExecuteRiskDownload(
                        _absolute_config_path(raw_config_path),
                        progress=risk_progress,
                        include_official_history=command == "risk_history_download",
                    )
                finally:
                    risk_progress.close()
        except ProcessLockError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "target_date": risk_result.target_date.isoformat(),
                    "download_scope": "history" if command == "risk_history_download" else "current",
                    "requested_codes": risk_result.requested_codes,
                    "available_codes": risk_result.available_codes,
                    "covered_codes": risk_result.covered_codes,
                    "partial_codes": risk_result.partial_codes,
                    "changed_codes": risk_result.changed_codes,
                    "deferred_codes": risk_result.deferred_codes,
                    "failed_codes": risk_result.failed_codes,
                    "deadline_reached": risk_result.deadline_reached,
                    "wave_count": risk_result.wave_count,
                    "stop_reason": risk_result.stop_reason,
                    "elapsed_seconds": round(risk_result.elapsed_seconds, 3),
                    "component_codes": {
                        "financial": risk_result.inventory.financial_codes,
                        "announcements": risk_result.inventory.announcement_codes,
                        "pledge": risk_result.inventory.pledge_codes,
                        "unlock": risk_result.inventory.unlock_codes,
                        "regulatory_history": risk_result.inventory.regulatory_history_codes,
                        "regulatory_evidence": risk_result.inventory.regulatory_evidence_codes,
                        "news_queried": risk_result.inventory.news_queried_codes,
                    },
                    "retained_items": {
                        "news": risk_result.inventory.news_items,
                        "announcements": risk_result.inventory.announcement_items,
                        "financial_reports": risk_result.inventory.financial_reports,
                    },
                    "official_source": {
                        "pages_fetched": risk_result.official_pages_fetched,
                        "pages_checkpointed": risk_result.official_pages_checkpointed,
                        "announcements_saved": risk_result.official_announcements_saved,
                        "resumed_codes": risk_result.official_resumed_codes,
                        "failed_codes": risk_result.official_failed_codes,
                        "persistence_error": risk_result.official_persistence_error,
                        "source_error": risk_result.official_source_error,
                    },
                },
                ensure_ascii=False,
            )
        )
        return 1 if risk_result.needs_retry else 0
    if command == "qfq_download":
        if profile is not None:
            parser.error("qfq_download does not accept --profile")
        from trader.bootstrap import ExecuteQfqDownload

        runtime = LoadRuntimeSettings(_absolute_config_path(raw_config_path))
        qfq_result = ExecuteQfqDownload(
            runtime.project_root,
            reference_root=runtime.reference_dir,
        )
        print(
            json.dumps(
                {
                    "target_date": qfq_result.target_date.isoformat() if qfq_result.target_date else None,
                    "completed_codes": qfq_result.completed_codes,
                    "pending_codes": qfq_result.pending_codes,
                    "no_trade_data_codes": qfq_result.no_trade_data_codes,
                    "latest_missing_codes": qfq_result.latest_missing_codes,
                    "changed_files": qfq_result.changed_files,
                    "changed_rows": qfq_result.changed_rows,
                    "skipped_codes": qfq_result.skipped_codes,
                    "failure_reason": qfq_result.failure_reason,
                    "pending_reasons": [
                        {"reason": item.reason, "count": item.count} for item in qfq_result.pending_reasons
                    ],
                },
                ensure_ascii=False,
            )
        )
        return 1 if qfq_result.needs_retry else 0
    if command == "download":
        if profile is not None:
            parser.error("download does not accept --profile")
        return _run_history_download(_absolute_config_path(raw_config_path))
    if command == "history-automation-status":
        from trader.download.entrypoints.commands import RunDownloadCommand

        return RunDownloadCommand(command)
    if command != "scheduled-history-maintenance":
        return None
    if profile is not None:
        parser.error(f"{command} does not accept --profile")
    config_path = _absolute_config_path(raw_config_path)
    return _run_scheduled_history_maintenance(config_path)


def _run_history_download(config_path: Path) -> int:
    from trader.download.entrypoints.commands import RunDownloadCommand

    return RunDownloadCommand("download", config_path=config_path)


def _run_scheduled_history_maintenance(config_path: Path) -> int:
    from trader.download.entrypoints.commands import RunDownloadCommand

    return RunDownloadCommand("scheduled-history-maintenance", config_path=config_path)


def _shanghai_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _run_config_validation(
    runtime: RuntimeSettings,
    profile_override: ScoringProfileId | None,
) -> int:
    strategy = LoadStrategySettings(
        runtime.strategy_config_path,
        scoring_profile=profile_override,
    )
    watchlist = LoadLongWatchlist(runtime.long_watchlist_path)
    from trader.download.infra.download_settings import LoadHistorySyncConfiguration
    from trader.recommendation.infra.scoring.cost_settings import (
        LoadEvaluationCostScenarios,
        LoadExecutionCostPolicy,
    )
    from trader.training.infra.research_settings import LoadResearchSettings
    from trader.training.infra.training_settings import LoadProfileTrainingSettings

    config_dir = runtime.config_path.parent
    download = LoadHistorySyncConfiguration(runtime.project_root, runtime.reference_dir)
    training_v2 = LoadProfileTrainingSettings(runtime.project_root, "v2")
    training_v3 = LoadProfileTrainingSettings(runtime.project_root, "v3")
    research = LoadResearchSettings(config_dir)
    cost = LoadExecutionCostPolicy(config_dir)
    cost_scenarios = LoadEvaluationCostScenarios(config_dir)
    print(
        json.dumps(
            {
                "status": "ok",
                "runtime_version": runtime.config_version,
                "strategy_version": strategy.strategy_version,
                "scoring_profile": strategy.scoring_profile,
                "watchlist_version": watchlist.watchlist_version,
                "runtime_dir": str(runtime.runtime_dir),
                "reference_dir": str(runtime.reference_dir),
                "history_sessions": download.sessions,
                "training_fit_dates": {"v2": training_v2.fit_dates, "v3": training_v3.fit_dates},
                "research_arms": len(research.ablation_arms),
                "terminal_holdout_sessions": research.terminal_holdout_sessions,
                "online_round_trip_base_rate": cost.round_trip_base_rate,
                "evaluation_round_trip_bps": [round(item.round_trip_return * 10_000) for item in cost_scenarios],
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
    eligibility_index = SQLiteIssuerEligibilityIndex(runtime.reference_dir, read_only=True)
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
    return LoadStrategySettings(runtime.strategy_config_path).scoring_profile


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
    return Main(argv)


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
    raise SystemExit(Main())
