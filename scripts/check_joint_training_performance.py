#!/usr/bin/env python3
"""Compare serial and concurrent V2/V3 fitting on one external history copy."""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

from trader.infra.atomic_files.json import AtomicWriteJson
from trader.training.application.tomorrow_training import TOMORROW_TRAINING_COMPUTE_THREADS

for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[name] = str(TOMORROW_TRAINING_COMPUTE_THREADS)

from trader.download.infra.history_storage_profile import ClassifyHistoryStorage  # noqa: E402
from trader.training.entrypoints.tomorrow_training_progress import StderrTomorrowTrainingProgress  # noqa: E402
from trader.training.infra.engine import (  # noqa: E402
    ParallelFitResourcesAvailable,
    ProfileTrainingRequest,
    RunJointProfileTraining,
)
from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot  # noqa: E402
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE  # noqa: E402
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE  # noqa: E402
from trader.training.infra.training_settings import LoadProfileTrainingSettings  # noqa: E402

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def BuildParser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--history-root", type=Path, required=True, help="Published history in an external isolated copy."
    )
    parser.add_argument("--workspace-root", type=Path, required=True, help="External temporary benchmark workspace.")
    parser.add_argument("--output", type=Path, required=True, help="External benchmark evidence JSON path.")
    return parser


def Main(argv: list[str] | None = None) -> int:
    args = BuildParser().parse_args(argv)
    history_root = _external_path(args.history_root)
    workspace_root = _external_path(args.workspace_root)
    output = _external_path(args.output)
    if (
        history_root == workspace_root
        or history_root in workspace_root.parents
        or workspace_root in history_root.parents
    ):
        raise ValueError("benchmark workspace must be separate from the history copy")
    if output == history_root or history_root in output.parents:
        raise ValueError("benchmark output must be separate from the history copy")
    AtomicWriteJson(output, {"schema_version": "joint_fit_benchmark", "status": "running"})
    if not ParallelFitResourcesAvailable(history_root):
        return _publish_rejected(output, "resource_ineligible", exit_code=2)
    os.nice(10)
    snapshot_hash = SQLiteHistoryTrainingInputSnapshot.open(history_root).snapshot.active_snapshot_hash
    workspace_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="joint-fit-benchmark.", dir=workspace_root) as temporary_name:
        temporary = Path(temporary_name)
        serial_first = _run_benchmark(history_root, temporary / "serial-first", snapshot_hash, parallel_fit=False)
        if any(result.status != "engineering_ready" for result in serial_first.profiles):
            return _publish_rejected(output, "serial_failed", exit_code=1)
        parallel = _run_benchmark(history_root, temporary / "parallel", snapshot_hash, parallel_fit=True)
        if any(result.status != "engineering_ready" for result in parallel.profiles):
            return _publish_rejected(output, "parallel_failed", exit_code=1)
        serial_last = _run_benchmark(history_root, temporary / "serial-last", snapshot_hash, parallel_fit=False)
    serial_hashes = tuple(result.training_input_hash for result in (*serial_first.profiles, *serial_last.profiles))
    parallel_hashes = tuple(result.training_input_hash for result in parallel.profiles)
    peak_rss_bytes = _peak_rss_bytes()
    best_serial_seconds = min(serial_first.fit_seconds, serial_last.fit_seconds)
    passed = (
        all(result.status == "engineering_ready" for result in (*serial_first.profiles, *serial_last.profiles))
        and len(set((*serial_hashes, *parallel_hashes))) == 1
        and serial_hashes[0] == snapshot_hash
        and best_serial_seconds > 0
        and parallel.fit_seconds > 0
        and parallel.fit_seconds <= 0.9 * best_serial_seconds
        and peak_rss_bytes <= 5 * 1024**3
    )
    payload = {
        "schema_version": "joint_fit_benchmark",
        "status": "passed" if passed else "failed",
        "storage_kind": ClassifyHistoryStorage(history_root),
        "cpu_count": os.cpu_count() or 1,
        "serial_input_hash": serial_hashes[0],
        "parallel_input_hash": parallel_hashes[0],
        "serial_fit_seconds": best_serial_seconds,
        "parallel_fit_seconds": parallel.fit_seconds,
        "parallel_peak_rss_bytes": peak_rss_bytes,
    }
    AtomicWriteJson(output, payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if passed else 1


def _publish_rejected(output: Path, status: str, *, exit_code: int) -> int:
    payload = {"schema_version": "joint_fit_benchmark", "status": status}
    AtomicWriteJson(output, payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


def _run_benchmark(history_root: Path, train_root: Path, snapshot_hash: str, *, parallel_fit: bool):
    with ExitStack() as progress_scope:
        profiles = (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE)
        progresses = tuple(
            progress_scope.enter_context(
                StderrTomorrowTrainingProgress(
                    command_label=f"{'并行' if parallel_fit else '串行'}-{profile.profile_id.upper()}训练"
                )
            )
            for profile in profiles
        )
        requests = tuple(
            ProfileTrainingRequest(
                history_root,
                train_root,
                profile,
                profile.heads,
                LoadProfileTrainingSettings(_PROJECT_ROOT, profile.profile_id),
                progress,
                expected_history_snapshot_hash=snapshot_hash,
            )
            for profile, progress in zip(profiles, progresses, strict=True)
        )
        result = RunJointProfileTraining((requests[0], requests[1]), parallel_fit=parallel_fit)
        for progress, profile_result in zip(progresses, result.profiles, strict=True):
            failure = next((reason for head in profile_result.heads for reason in head.failure_reasons), None)
            progress.publish_result(profile_result.status, failure)
        return result


def _external_path(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("benchmark paths must be absolute and external to the repository")
    resolved = path.resolve()
    if resolved == _PROJECT_ROOT or _PROJECT_ROOT in resolved.parents:
        raise ValueError("benchmark paths must be external to the repository")
    return resolved


def _peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


__all__ = ["BuildParser", "Main"]


if __name__ == "__main__":
    raise SystemExit(Main())
