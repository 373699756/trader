"""Parse explicit research commands; execution and dependencies belong to bootstrap."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trader.training.application.capability_completion import CapabilityExecution
    from trader.training.application.cross_strategy_conclusion import CrossStrategyConclusion
    from trader.training.domain.evaluation.historical_industry_facts import HistoricalIndustryDatasetReport
    from trader.training.domain.evaluation.point_in_time_data_qualification import PointInTimeDataQualificationReport

    ResearchEvidenceResult = (
        CapabilityExecution
        | CrossStrategyConclusion
        | HistoricalIndustryDatasetReport
        | PointInTimeDataQualificationReport
    )


@dataclass(frozen=True)
class CapabilityCommand:
    runtime_dir: Path
    artifact_dir: Path
    code: str
    anchor_date: date
    timeout_seconds: float


@dataclass(frozen=True)
class QualificationCommand:
    history_root: Path
    code: str
    anchor_date: date
    timeout_seconds: float


@dataclass(frozen=True)
class IndustryAuditCommand:
    history_root: Path
    required_sample_codes: int
    tushare_access_points: int


@dataclass(frozen=True)
class HoldoutCommand:
    parent_artifact_dir: Path
    output_dir: Path


ResearchEvidenceCommand = CapabilityCommand | QualificationCommand | IndustryAuditCommand | HoldoutCommand
COMMAND_SCHEMAS = {
    "research-h1-capability": ("h1_capability_execution", "probe_failed"),
    "research-data-qualification": ("point_in_time_data_qualification", "probe_failed"),
    "research-industry-audit": ("historical_industry_dataset", "audit_failed"),
    "research-terminal-holdout": ("terminal_holdout_execution", "execution_failed"),
}


def add_research_evidence_parsers(subparsers: argparse._SubParsersAction) -> None:
    descriptions = (
        ("research-h1-capability", "Probe two suppliers and seal fail-closed H1 evidence outside the repository."),
        ("research-data-qualification", "Read archive evidence and probe two suppliers without archive writes."),
        (
            "research-industry-audit",
            "Audit archived industry facts offline; detailed output requires an external path.",
        ),
        ("research-terminal-holdout", "Seal two terminal reports and a conclusion outside the repository; offline."),
    )
    for name, description in descriptions:
        parser = subparsers.add_parser(name, help=description, description=description)
        parser.add_argument("--output", default="-", help="stdout or an absolute repository-external report path")
        if name in {"research-h1-capability", "research-data-qualification"}:
            parser.add_argument("--code", default="600519", help="single six-digit sample; omitted from summaries")
            parser.add_argument("--historical-anchor-date", type=date.fromisoformat, default=date(2022, 1, 4))
            parser.add_argument(
                "--timeout-seconds",
                type=float,
                default=8.0,
                help="per-request timeout in (0,60]; two attempts total, no retries or proxy fallback",
            )
        if name == "research-h1-capability":
            parser.add_argument("--h1-runtime-dir", type=Path, required=True)
            parser.add_argument("--artifact-dir", type=Path, required=True)
        elif name == "research-terminal-holdout":
            parser.add_argument("--parent-artifact-dir", type=Path, required=True)
            parser.add_argument("--output-dir", type=Path, required=True)
        else:
            parser.add_argument("--history-root", type=Path, required=True, help="explicit read-only history root")
        if name == "research-industry-audit":
            parser.add_argument("--required-sample-codes", type=int, default=300)
            parser.add_argument("--tushare-access-points", type=int, default=0, help="metadata only; no Tushare call")
            parser.add_argument("--include-details", action="store_true")


def external_path(value: Path, *, repository_root: Path, option: str) -> Path:
    path = value.expanduser()
    if not path.is_absolute():
        raise ValueError(f"{option} must be an absolute repository-external path")
    resolved = path.resolve()
    if resolved == repository_root or repository_root in resolved.parents:
        raise ValueError(f"{option} must be outside the repository")
    return resolved


def parse_evidence_command(args: argparse.Namespace, *, repository_root: Path) -> ResearchEvidenceCommand:
    if args.command in {"research-h1-capability", "research-data-qualification"}:
        from trader.training.domain.evaluation.h1_point_in_time import H1_SOURCE_CUTOFF

        if len(args.code) != 6 or not args.code.isascii() or not args.code.isdigit():
            raise ValueError("sample code must have six ASCII digits")
        if args.historical_anchor_date >= H1_SOURCE_CUTOFF:
            raise ValueError("historical anchor date exceeds source cutoff")
    if args.command == "research-h1-capability":
        return CapabilityCommand(
            args.h1_runtime_dir.expanduser().resolve(),
            external_path(args.artifact_dir, repository_root=repository_root, option="--artifact-dir"),
            args.code,
            args.historical_anchor_date,
            _timeout(args.timeout_seconds),
        )
    if args.command == "research-data-qualification":
        return QualificationCommand(
            args.history_root.expanduser().resolve(),
            args.code,
            args.historical_anchor_date,
            _timeout(args.timeout_seconds),
        )
    if args.command == "research-industry-audit":
        if args.required_sample_codes <= 0 or args.tushare_access_points < 0:
            raise ValueError("industry sample count must be positive and access points non-negative")
        return IndustryAuditCommand(
            args.history_root.expanduser().resolve(), args.required_sample_codes, args.tushare_access_points
        )
    if args.command == "research-terminal-holdout":
        parent = external_path(
            args.parent_artifact_dir, repository_root=repository_root, option="--parent-artifact-dir"
        )
        output = external_path(args.output_dir, repository_root=repository_root, option="--output-dir")
        if parent == output or parent in output.parents or output in parent.parents:
            raise ValueError("terminal parent and output directories must be separate")
        return HoldoutCommand(parent, output)
    raise ValueError("unknown research evidence command")


def _timeout(value: float) -> float:
    if not math.isfinite(value) or not 0 < value <= 60:
        raise ValueError("supplier timeout must be finite and in (0,60]")
    return value


def run_research_evidence(args: argparse.Namespace, *, repository_root: Path) -> int:
    import requests

    from trader.bootstrap import execute_research_evidence
    from trader.training.entrypoints.research_evidence_projection import project_evidence_result

    output: Path | None = None
    try:
        if args.output != "-":
            output = external_path(Path(args.output), repository_root=repository_root, option="--output")
        include_details = args.command == "research-industry-audit" and args.include_details
        if include_details and output is None:
            raise ValueError("--include-details requires an external --output path")
        command = parse_evidence_command(args, repository_root=repository_root)
        _validate_output_separation(command, output)
        report = execute_research_evidence(command)
        payload, exit_code = project_evidence_result(report, include_details=include_details)
        _write(payload, output)
        return exit_code
    except (OSError, RuntimeError, TypeError, ValueError, requests.RequestException) as exc:
        schema, status = COMMAND_SCHEMAS[args.command]
        payload = {
            "schema_version": schema,
            "status": status,
            "error_code": _error_code(exc, args.command),
            "production_authority": False,
        }
        if args.command == "research-industry-audit":
            payload["training_authority"] = False
        # Never retry a failed report write against the same destination.
        _write(payload, None)
        return 2


def _validate_output_separation(command: ResearchEvidenceCommand, summary: Path | None) -> None:
    roots: tuple[Path, ...]
    if isinstance(command, CapabilityCommand):
        roots = (command.runtime_dir, command.artifact_dir)
        if _overlap(command.runtime_dir, command.artifact_dir):
            raise ValueError("H1 input and artifact directories must be separate")
    elif isinstance(command, HoldoutCommand):
        roots = (command.parent_artifact_dir, command.output_dir)
    else:
        roots = (command.history_root,)
    if summary is not None and any(summary == root or root in summary.parents for root in roots):
        raise ValueError("summary output must be separate from evidence directories")


def _overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _write(payload: dict[str, object], output: Path | None) -> None:
    rendered = json.dumps(payload, ensure_ascii=True, sort_keys=True, allow_nan=False) + "\n"
    if output is None:
        print(rendered, end="", flush=True)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")


def _error_code(exc: BaseException, command: str) -> str:
    import requests

    if isinstance(exc, requests.Timeout):
        return "source_timeout"
    if isinstance(exc, requests.RequestException):
        return "source_request_failed"
    if isinstance(exc, OSError):
        return "artifact_io_failed"
    if command == "research-industry-audit" and any(word in str(exc).lower() for word in ("manifest", "partition")):
        return "daily_archive_invalid"
    if isinstance(exc, RuntimeError) and command in {"research-h1-capability", "research-terminal-holdout"}:
        return "artifact_conflict"
    return {
        "research-h1-capability": "invalid_capability_evidence",
        "research-data-qualification": "invalid_qualification_evidence",
        "research-industry-audit": "invalid_industry_evidence",
        "research-terminal-holdout": "invalid_parent_artifacts",
    }[command]
