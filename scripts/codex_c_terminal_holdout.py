#!/usr/bin/env python3
"""Seal Codex C terminal holdout outcomes from an immutable Codex A parent."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from trader.application.research.cross_strategy_conclusion import (  # noqa: E402
    CrossStrategyConclusion,
    CrossStrategyConclusionService,
)
from trader.application.research.d25_terminal_holdout import D25TerminalHoldoutService  # noqa: E402
from trader.application.research.today_terminal_holdout import TodayTerminalHoldoutService  # noqa: E402
from trader.application.research.tomorrow_point_in_time_holdout import (  # noqa: E402
    TomorrowPointInTimeHoldoutService,
)
from trader.domain.research.h1_point_in_time import H1CapabilityAuditReport, H1Strategy  # noqa: E402
from trader.domain.research.terminal_holdout import (  # noqa: E402
    TerminalHoldoutParentState,
    TerminalHoldoutReport,
)
from trader.infra.research.cross_strategy_conclusion_artifacts import (  # noqa: E402
    CrossStrategyConclusionArtifactStore,
)
from trader.infra.research.d25_terminal_holdout_artifacts import (  # noqa: E402
    D25TerminalHoldoutArtifactStore,
)
from trader.infra.research.h1_point_in_time_capability import (  # noqa: E402
    H1CapabilityArtifactStore,
)
from trader.infra.research.h1_point_in_time_completion import (  # noqa: E402
    CodexACompletionArtifactIndex,
    CodexACompletionArtifactStore,
)
from trader.infra.research.historical_label_artifacts import HistoricalLabelArtifactStore  # noqa: E402
from trader.infra.research.today_terminal_holdout_artifacts import (  # noqa: E402
    TodayTerminalHoldoutArtifactStore,
)
from trader.infra.research.tomorrow_point_in_time_holdout_artifacts import (  # noqa: E402
    TomorrowPointInTimeHoldoutArtifactStore,
)


@dataclass(frozen=True)
class _ParentArtifacts:
    capability: H1CapabilityAuditReport
    index: CodexACompletionArtifactIndex


def execute(*, parent_artifact_dir: Path, output_dir: Path) -> CrossStrategyConclusion:
    parent = _read_parent(parent_artifact_dir)
    statuses = {item.strategy: item for item in parent.capability.strategies}
    residual_hashes = dict(parent.index.residual_terminal_hashes)

    today = TodayTerminalHoldoutService(
        (),
        _parent_state(parent, statuses["today"].strategy, residual_hashes["today"]),
    ).execute()
    tomorrow = TomorrowPointInTimeHoldoutService(
        (),
        _parent_state(parent, statuses["tomorrow"].strategy, parent.index.c3_terminal_hash),
    ).execute()
    d25 = D25TerminalHoldoutService(
        (),
        _parent_state(parent, statuses["d25"].strategy, residual_hashes["d25"]),
    ).execute()

    TodayTerminalHoldoutArtifactStore(output_dir / "today").write(today)
    TomorrowPointInTimeHoldoutArtifactStore(output_dir / "tomorrow").write(tomorrow)
    D25TerminalHoldoutArtifactStore(output_dir / "d25").write(d25)
    conclusion = CrossStrategyConclusionService().execute(today, tomorrow, d25)
    CrossStrategyConclusionArtifactStore(output_dir / "cross_strategy").write(conclusion)
    return conclusion


def _read_parent(root: Path) -> _ParentArtifacts:
    capability = H1CapabilityArtifactStore(root).verify()
    labels = HistoricalLabelArtifactStore(root).verify()
    index = CodexACompletionArtifactStore(root).verify()
    if index.capability_hash != capability.content_hash:
        raise ValueError("Codex C parent capability hash does not match terminal index")
    if index.label_batch_hash != labels.content_hash:
        raise ValueError("Codex C parent label hash does not match terminal index")
    return _ParentArtifacts(capability, index)


def _parent_state(parent: _ParentArtifacts, strategy: H1Strategy, candidate_hash: str) -> TerminalHoldoutParentState:
    status = next(item for item in parent.capability.strategies if item.strategy == strategy)
    return TerminalHoldoutParentState(
        candidate_status=status.state,
        parent_hash=parent.capability.content_hash,
        candidate_hash=candidate_hash,
        failure_reasons=tuple((*status.failure_reasons, *parent.capability.probe_failures)),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-artifact-dir", type=Path, required=True, help="Codex A artifact directory")
    parser.add_argument("--output-dir", type=Path, required=True, help="absolute output directory outside repository")
    parser.add_argument("--output", default="-", help="sanitized JSON output path outside repository, or -")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        parent = _external_path(args.parent_artifact_dir, "--parent-artifact-dir")
        output_dir = _external_path(args.output_dir, "--output-dir")
        output = args.output if args.output == "-" else str(_external_path(Path(args.output), "--output"))
        conclusion = execute(parent_artifact_dir=parent, output_dir=output_dir)
        payload = _projection(conclusion)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        payload = {
            "schema_version": "terminal_holdout_execution",
            "status": "execution_failed",
            "error_code": _error_code(exc),
            "production_authority": False,
        }
        output = "-"
        _write_output(payload, output)
        return 2
    _write_output(payload, output)
    return 0 if conclusion.status == "historical_validated" else 1


def _projection(conclusion: CrossStrategyConclusion) -> dict[str, object]:
    reports = (conclusion.today, conclusion.tomorrow, conclusion.d25)
    return {
        "schema_version": "terminal_holdout_execution",
        "status": conclusion.status,
        "conclusion_hash": conclusion.content_hash,
        "report_hashes": [list(item) for item in conclusion.report_hashes],
        "strategies": [_report_projection(report) for report in reports],
        "production_authority": False,
    }


def _report_projection(report: TerminalHoldoutReport) -> dict[str, object]:
    return {
        "strategy": report.strategy,
        "status": report.status,
        "research_identity": report.research_identity,
        "parent_hash": report.parent_hash,
        "candidate_hash": report.candidate_hash,
        "terminal_holdout_opened": report.terminal_holdout_opened,
        "failure_reasons": list(report.failure_reasons),
        "report_hash": report.content_hash,
    }


def _external_path(value: Path, option: str) -> Path:
    if not value.expanduser().is_absolute():
        raise ValueError(f"{option} must be an absolute path outside the repository")
    resolved = value.expanduser().resolve()
    if resolved == PROJECT_ROOT or PROJECT_ROOT in resolved.parents:
        raise ValueError(f"{option} must be an absolute path outside the repository")
    return resolved


def _write_output(payload: dict[str, object], output: str) -> None:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    if output == "-":
        print(encoded)
        return
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encoded + "\n", encoding="utf-8")


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, OSError):
        return "artifact_io_failed"
    if isinstance(exc, RuntimeError):
        return "artifact_conflict"
    return "invalid_parent_artifacts"


if __name__ == "__main__":
    raise SystemExit(main())
