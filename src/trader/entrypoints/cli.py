"""current configuration, performance, and explicit research command entrypoint."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from trader.download.infra.akshare_calendar import AkshareCalendarTimeoutError
from trader.infra.settings import LoadLongWatchlist, LoadRuntimeSettings, LoadStrategySettings, RuntimeSettings
from trader.infra.signal_cancellation import ManualCancellation
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS, ScoringProfileId
from trader.recommendation.domain.scoring.profile_selection import ParseProfileSelection
from trader.recommendation.infra.persistence.issuer_eligibility import SQLiteIssuerEligibilityIndex
from trader.training.entrypoints.research_evidence import COMMAND_SCHEMAS, AddResearchEvidenceParsers


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
    training = subparsers.add_parser("train", help="Maintain history once, then train all or the selected profiles.")
    training.add_argument(
        "profiles",
        nargs="?",
        type=ParseProfileSelection,
        default=SCORING_PROFILE_IDS,
        help="Comma-separated profiles; defaults to all profiles.",
    )
    sync = subparsers.add_parser("sync", help="Prepare references, then synchronize history, market and company.")
    sync.add_argument("target", nargs="?", default="all", choices=("all", "market", "company", "history"))
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
    try:
        config_path = _absolute_config_path(args.config)
        runtime = LoadRuntimeSettings(config_path)
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "failed", "failure": "configuration_invalid", "category": type(exc).__name__}))
        return 2
    profile_override = cast(ScoringProfileId | None, args.profile)
    if args.command != "validate-config":
        from trader.bootstrap import PrepareRuntimeStorage

        PrepareRuntimeStorage(runtime)
    if args.command in ("sync", "train"):
        if args.profile is not None:
            parser.error(f"{args.command} does not accept --profile")
        try:
            _configuration_payload(runtime, None)
        except (OSError, RuntimeError, ValueError) as exc:
            print(json.dumps({"status": "failed", "failure": "configuration_invalid", "category": type(exc).__name__}))
            return 2
        print(f"{args.command}: 正在准备官方总体、ST资格、交易日历及当前行业。", file=sys.stderr)
        if args.command == "train":
            profiles = cast(tuple[ScoringProfileId, ...], args.profiles)
            print(f"train: 本次训练档位 {', '.join(profile.upper() for profile in profiles)}。", file=sys.stderr)
            return _run_history_download(config_path, profiles)
        from trader.bootstrap import ExecuteSync
        from trader.download.entrypoints.sync_projection import ProjectSyncResult

        with ManualCancellation() as cancelled:
            result = ExecuteSync(config_path, args.target, cancel_requested=cancelled.is_set)
        print(json.dumps(ProjectSyncResult(result), ensure_ascii=False, sort_keys=True))
        return result.exit_code
    if args.command == "history-automation-status":
        from trader.bootstrap import ExecuteTrainingReminderStatus
        from trader.training.entrypoints.training_reminder_projection import ProjectTrainingReminderStatus

        status = ExecuteTrainingReminderStatus(runtime)
        print(json.dumps(ProjectTrainingReminderStatus(status), ensure_ascii=False, sort_keys=True))
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
        return _run_eligibility_list(runtime, as_of=args.as_of)
    if args.command.startswith("research-"):
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
        args.command,
        config_path,
        runtime,
        ResearchCommandOptions(
            workers=int(getattr(args, "workers", 5)), profile=cast(ScoringProfileId | None, args.profile)
        ),
    )


def _run_history_download(config_path: Path, profiles: tuple[ScoringProfileId, ...]) -> int:
    from trader.training.entrypoints.commands import ConfigureTrainingResources

    ConfigureTrainingResources()
    from trader.bootstrap import ExecuteDownloadTrain

    try:
        return ExecuteDownloadTrain(config_path, scoring_profiles=profiles)
    except KeyboardInterrupt:
        print(json.dumps({"schema_version": "selected_training_result", "status": "cancelled"}))
        return 130
    except AkshareCalendarTimeoutError as exc:
        print(
            f"train: 交易日历准备超时，AkShare/Sina 在 {exc.timeout_seconds:g} 秒内未完成；"
            "尚未进入模型训练，请稍后重试。",
            file=sys.stderr,
        )
        print(
            json.dumps(
                {
                    "schema_version": "selected_training_result",
                    "status": "failed",
                    "category": "TimeoutError",
                    "stage": "reference_calendar",
                    "source": "akshare_sina",
                    "timeout_seconds": exc.timeout_seconds,
                }
            )
        )
        return 1
    except (OSError, RuntimeError, ValueError) as exc:
        print(
            json.dumps(
                {"schema_version": "selected_training_result", "status": "failed", "category": type(exc).__name__}
            )
        )
        return 1


def _shanghai_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _run_config_validation(runtime: RuntimeSettings, profile_override: ScoringProfileId | None) -> int:
    print(json.dumps(_configuration_payload(runtime, profile_override), ensure_ascii=False, sort_keys=True))
    return 0


def _configuration_payload(runtime: RuntimeSettings, profile_override: ScoringProfileId | None) -> dict[str, object]:
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
    from trader.training.infra.profile.v1.settings import LoadV1TrainingSettings
    from trader.training.infra.profile.v5.configuration import TcnConfigurationDocument
    from trader.training.infra.profile.v5.settings import TcnFittingParameters
    from trader.training.infra.research_settings import LoadResearchSettings
    from trader.training.infra.training_settings import LoadProfileTrainingSettings

    config_dir = runtime.config_path.parent
    download = LoadHistorySyncConfiguration(runtime.project_root, runtime.data_paths)
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
        "history_sessions": download.sessions,
        "training_fit_dates": fit_dates,
        "v5_training_configuration": TcnConfigurationDocument(training_v5.fit_dates, training_v5.fitting),
        "research_arms": len(research.ablation_arms),
        "terminal_holdout_sessions": research.terminal_holdout_sessions,
        "online_round_trip_base_rate": cost.round_trip_base_rate,
        "evaluation_round_trip_bps": [round(item.round_trip_return * 10_000) for item in cost_scenarios],
    }


def _run_eligibility_list(runtime: RuntimeSettings, *, as_of: str | None) -> int:
    observed_at = datetime.fromisoformat(as_of) if as_of else datetime.now(ZoneInfo("Asia/Shanghai"))
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise SystemExit("--as-of must include a timezone offset")
    observed_at = observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
    eligibility_index = SQLiteIssuerEligibilityIndex(runtime.data_paths.eligibility, read_only=True)
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
