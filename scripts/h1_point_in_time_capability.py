#!/usr/bin/env python3
"""Probe free H1 source capability and seal the CodexA fail-closed terminal chain."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Protocol

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from trader.application.research.h1_point_in_time_completion import (  # noqa: E402
    CodexAResearchCompletion,
    complete_codex_a_research,
)
from trader.domain.research.h1_point_in_time import H1CapabilityAuditReport, H1PointInTimeSpec  # noqa: E402
from trader.infra.research.h1_point_in_time_archive import SQLiteH1PointInTimeArchive  # noqa: E402
from trader.infra.research.h1_point_in_time_capability import (  # noqa: E402
    FreeSourceH1CapabilityProbe,
    H1CapabilityArtifactStore,
    H1HTTPSession,
)
from trader.infra.research.h1_point_in_time_completion import (  # noqa: E402
    CodexACompletionArtifactIndex,
    CodexACompletionArtifactStore,
)
from trader.infra.research.historical_label_artifacts import HistoricalLabelArtifactStore  # noqa: E402


class _SessionFactory(Protocol):
    def __call__(self) -> H1HTTPSession: ...


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--h1-runtime-dir", type=Path, required=True, help="existing H1 archive runtime directory")
    parser.add_argument(
        "--artifact-dir", type=Path, required=True, help="absolute repository-external artifact directory"
    )
    parser.add_argument("--code", default="600519", help="one representative six-digit A-share code")
    parser.add_argument(
        "--historical-anchor-date",
        type=date.fromisoformat,
        default=date(2022, 1, 4),
        help="old trading date used to detect providers that ignore historical minute bounds",
    )
    parser.add_argument("--timeout-seconds", type=float, default=8.0, help="bounded timeout per supplier request")
    parser.add_argument("--output", default="-", help="sanitized JSON output path outside the repository, or -")
    return parser


def execute(
    *,
    h1_runtime_dir: Path,
    artifact_dir: Path,
    code: str,
    historical_anchor_date: date,
    timeout_seconds: float,
    session: H1HTTPSession,
) -> tuple[H1CapabilityAuditReport, CodexAResearchCompletion, CodexACompletionArtifactIndex]:
    capability = FreeSourceH1CapabilityProbe(session, timeout_seconds=timeout_seconds).run(
        code=code,
        historical_anchor_date=historical_anchor_date,
    )
    archive = SQLiteH1PointInTimeArchive(h1_runtime_dir)
    metadata = tuple(archive.label_metadata(H1PointInTimeSpec(strategy)) for strategy in ("today", "tomorrow", "d25"))
    completion = complete_codex_a_research(capability=capability, metadata=metadata)
    H1CapabilityArtifactStore(artifact_dir).write(capability)
    HistoricalLabelArtifactStore(artifact_dir).write(completion.labels)
    index = CodexACompletionArtifactStore(artifact_dir).write(completion)
    return capability, completion, index


def main(argv: list[str] | None = None, *, session_factory: _SessionFactory = requests.Session) -> int:
    args = _parser().parse_args(argv)
    try:
        artifact_dir = _external_path(args.artifact_dir, "--artifact-dir")
        output = args.output if args.output == "-" else str(_external_path(Path(args.output), "--output"))
        capability, completion, index = execute(
            h1_runtime_dir=args.h1_runtime_dir.expanduser().resolve(),
            artifact_dir=artifact_dir,
            code=args.code,
            historical_anchor_date=args.historical_anchor_date,
            timeout_seconds=args.timeout_seconds,
            session=session_factory(),
        )
        payload = _projection(capability, completion, index)
    except (OSError, RuntimeError, TypeError, ValueError, requests.RequestException) as exc:
        payload = {
            "schema_version": "codex_a_h1_capability_execution_v1",
            "status": "probe_failed",
            "error_code": _error_code(exc),
            "production_authority": False,
        }
        output = "-"
        _write_output(payload, output)
        return 2
    _write_output(payload, output)
    return 1 if completion.status == "historical_data_insufficient" else 0


def _projection(
    capability: H1CapabilityAuditReport,
    completion: CodexAResearchCompletion,
    index: CodexACompletionArtifactIndex,
) -> dict[str, object]:
    return {
        "schema_version": "codex_a_h1_capability_execution_v1",
        "status": completion.status,
        "capability_hash": capability.content_hash,
        "completion_hash": completion.content_hash,
        "terminal_index_hash": index.content_hash,
        "sources": [
            {
                "source": item.source,
                "earliest_available": item.earliest_available.isoformat() if item.earliest_available else None,
                "returned_history_rows": item.page_size,
                "supports_today_1120": item.supports_today_1120,
                "supports_1450": item.supports_1450,
                "effective_security_state": item.security_state_effective_at,
                "estimated_requests": item.estimated_requests,
            }
            for item in capability.probes
        ],
        "strategies": [
            {
                "strategy": item.strategy,
                "state": item.state,
                "failure_reasons": list(item.failure_reasons),
                "terminal_holdout_opened": item.terminal_holdout_opened,
            }
            for item in capability.strategies
        ],
        "residual_terminal_hashes": [list(item) for item in index.residual_terminal_hashes],
        "c3_terminal_hash": index.c3_terminal_hash,
        "oof_generated": completion.c3.oof_artifact_hash is not None,
        "model_generated": completion.c3.candidate_model_artifact_hash is not None,
        "production_authority": False,
        "automatic_model_update": False,
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
    if isinstance(exc, requests.Timeout):
        return "source_timeout"
    if isinstance(exc, requests.RequestException):
        return "source_request_failed"
    if isinstance(exc, OSError):
        return "artifact_io_failed"
    if isinstance(exc, RuntimeError):
        return "artifact_conflict"
    return "invalid_capability_evidence"


if __name__ == "__main__":
    raise SystemExit(main())
