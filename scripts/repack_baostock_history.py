#!/usr/bin/env python3
"""Build and switch the fixed-path BaoStock archive to compact 8 KiB SQLite files."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from trader.infra.research.history_archive_repack import (
    HistoryArchiveRepackCoordinator,
    HistoryArchiveRepackError,
)
from trader.infra.research.history_archive_repack_state import HistoryArchiveRepackStatus

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = PROJECT_ROOT / "data/history/baostock"
DEFAULT_TARGET = PROJECT_ROOT / "data/historyless/baostock"
DEFAULT_TRAINING_ROOT = PROJECT_ROOT / "data/train"
DEFAULT_MEMORY_EVIDENCE = PROJECT_ROOT / "data/historyless/training-memory-result.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("build", "activate", "rollback", "finalize"):
        command = subparsers.add_parser(action)
        command.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
        command.add_argument("--target", type=Path, default=DEFAULT_TARGET)
        if action == "finalize":
            command.add_argument("--training-root", type=Path, default=DEFAULT_TRAINING_ROOT)
            command.add_argument("--memory-evidence", type=Path, default=DEFAULT_MEMORY_EVIDENCE)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    coordinator = HistoryArchiveRepackCoordinator(
        args.source,
        args.target,
        progress=_progress if args.action == "build" else None,
    )
    try:
        if args.action == "build":
            status = coordinator.build()
        elif args.action == "activate":
            status = coordinator.activate()
        elif args.action == "rollback":
            status = coordinator.rollback()
        else:
            status = coordinator.finalize(args.training_root, args.memory_evidence)
    except (HistoryArchiveRepackError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "schema_version": "history_archive_repack_status",
                    "state": "failed",
                    "error_code": _error_code(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(_project_status(status), ensure_ascii=False, sort_keys=True))
    return 0


def _project_status(status: HistoryArchiveRepackStatus) -> dict[str, object]:
    return {
        "schema_version": "history_archive_repack_status",
        "action": status.action,
        "state": status.state,
        "source_snapshot_hash": status.source_snapshot_hash,
        "target_snapshot_hash": status.target_snapshot_hash,
        "completed_partitions": status.completed_partitions,
        "total_partitions": status.total_partitions,
        "source_bytes": status.source_bytes,
        "target_bytes": status.target_bytes,
        "released_bytes": status.released_bytes,
    }


def _progress(completed: int, total: int, current: str) -> None:
    percent = completed / total * 100 if total else 0.0
    print(f"SQLite压实 | {completed}/{total} ({percent:.2f}%) | {current}", file=sys.stderr, flush=True)


def _error_code(exc: BaseException) -> str:
    text = str(exc).strip().lower().replace(" ", "_")
    return (
        text
        if text and len(text) <= 96 and all(character.isalnum() or character == "_" for character in text)
        else "history_archive_repack_failed"
    )


if __name__ == "__main__":
    raise SystemExit(main())
