"""Explicit foreground actions for the fixed-path history partition repack."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.core.contracts import ToolOutcome
from scripts.core.paths import PROJECT_ROOT

if TYPE_CHECKING:
    from trader.download.domain.history_control import HistoryRepackTrainingProof
    from trader.download.infra.history_partition_repack import HistoryPartitionRepackCoordinator
    from trader.download.infra.history_partition_repack_state import (
        HistoryPartitionRepackAction,
        HistoryPartitionRepackStatus,
    )


def _select_action(action_identity: str) -> HistoryPartitionRepackAction:
    actions: dict[str, HistoryPartitionRepackAction] = {
        "history-repack-build": "build",
        "history-repack-activate": "activate",
        "history-repack-rollback": "rollback",
        "history-repack-finalize": "finalize",
    }
    try:
        return actions[action_identity]
    except KeyError as exc:
        raise ValueError("unknown_history_repack_action") from exc


def _build_parser(action: HistoryPartitionRepackAction) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--runtime-config", type=Path, default=PROJECT_ROOT / "config/runtime.json")
    if action == "finalize":
        parser.add_argument("--training-root", type=Path, required=True)
        parser.add_argument("--memory-evidence", type=Path, required=True)
    return parser


def _coordinator(args: argparse.Namespace, action: HistoryPartitionRepackAction) -> HistoryPartitionRepackCoordinator:
    from trader.bootstrap import BuildSqlitePolicy
    from trader.download.infra.history_partition_repack import HistoryPartitionRepackCoordinator
    from trader.infra.settings import LoadRuntimeSettings

    return HistoryPartitionRepackCoordinator(
        args.source,
        args.target,
        progress=_progress if action == "build" else None,
        sqlite_policy=BuildSqlitePolicy(LoadRuntimeSettings(args.runtime_config)),
    )


def _verify_training(training_root: Path, memory_evidence: Path, snapshot_hash: str) -> HistoryRepackTrainingProof:
    from trader.training.infra.history.repack_training_evidence import VerifyHistoryRepackTraining

    return VerifyHistoryRepackTraining(training_root, memory_evidence, snapshot_hash)


def Run(action_identity: str, arguments: list[str]) -> ToolOutcome:
    """Keep stateful maintenance in the foreground without a diagnostic subprocess deadline."""
    action = _select_action(action_identity)
    args = _build_parser(action).parse_args(arguments)
    from trader.download.infra.history_partition_repack import HistoryPartitionRepackError

    try:
        coordinator = _coordinator(args, action)
        if action == "build":
            status = coordinator.build()
        elif action == "activate":
            status = coordinator.activate()
        elif action == "rollback":
            status = coordinator.rollback()
        else:
            status = coordinator.finalize(
                verify_training=lambda snapshot_hash: _verify_training(args.training_root, args.memory_evidence, snapshot_hash)
            )
    except (HistoryPartitionRepackError, OSError, ValueError) as exc:
        return ToolOutcome(1, {
            "schema_version": "history_archive_repack_status",
            "action": action,
            "status": "failed",
            "state": "failed",
            "error_code": _error_code(exc),
        })
    accepted_states = {
        "build": {"completed"},
        "activate": {"verified", "finalized"},
        "rollback": {"rolled_back"},
        "finalize": {"finalized"},
    }
    passed = status.action == action and status.state in accepted_states[action]
    payload = _project_status(status)
    payload["status"] = "passed" if passed else "failed"
    if not passed:
        payload["error_code"] = "history_repack_action_not_completed"
    return ToolOutcome(0 if passed else 1, payload)


def _project_status(status: HistoryPartitionRepackStatus) -> dict[str, object]:
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
