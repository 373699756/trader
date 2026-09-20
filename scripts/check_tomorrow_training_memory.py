#!/usr/bin/env python3
"""Run the fenced V3 training job and emit Tomorrow-compatible repack evidence."""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import tempfile
from pathlib import Path

from trader.infra.serialization.canonical import content_hash
from trader.training.application.tomorrow_training import (
    TOMORROW_TRAINING_COMPUTE_THREADS,
    TOMORROW_TRAINING_PEAK_RSS_MIB,
)


def _configure_resources() -> None:
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = str(TOMORROW_TRAINING_COMPUTE_THREADS)


_configure_resources()

from trader.training.entrypoints.tomorrow_training_progress import StderrTomorrowTrainingProgress  # noqa: E402
from trader.training.infra.profile.v3.training import run_repack_v3_training  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-root", type=Path, required=True)
    parser.add_argument("--train-root", type=Path, required=True)
    parser.add_argument("--expected-history-snapshot-hash", required=True, type=_sha256)
    parser.add_argument("--max-rss-mib", type=int, default=TOMORROW_TRAINING_PEAK_RSS_MIB)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/historyless/training-memory-result.json"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    os.nice(10)
    before = _peak_rss_bytes()
    with StderrTomorrowTrainingProgress() as progress:
        result = run_repack_v3_training(
            args.history_root.resolve(),
            args.train_root.resolve(),
            progress=progress,
            expected_history_snapshot_hash=args.expected_history_snapshot_hash,
        )
        stage_durations = progress.stage_durations
        repeated = run_repack_v3_training(
            args.history_root.resolve(),
            args.train_root.resolve(),
            progress=progress,
            expected_history_snapshot_hash=args.expected_history_snapshot_hash,
        )
    peak = _peak_rss_bytes()
    budget = args.max_rss_mib * 1024 * 1024
    tomorrow = next(head for head in result.heads if head.strategy.value == "tomorrow")
    passed = result.status == "engineering_ready" and repeated.status == "already_current" and peak <= budget
    payload: dict[str, object] = {
        "schema_version": "tomorrow_training_memory_gate",
        "status": "passed" if passed else "failed",
        "training_status": result.status,
        "repeat_training_status": repeated.status,
        "training_input_hash": result.training_input_hash,
        "model_hash": tomorrow.model_hash,
        "report_hash": tomorrow.report_hash,
        "peak_rss_bytes": peak,
        "starting_peak_rss_bytes": before,
        "max_rss_bytes": budget,
        "sample_database_peak_bytes": result.sample_database_peak_bytes,
        "stage_durations_ms": {name: round(seconds * 1_000.0, 1) for name, seconds in stage_durations},
        "failure_reasons": [reason for head in result.heads for reason in head.failure_reasons],
    }
    payload["content_hash"] = content_hash(payload)
    rendered = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n"
    _write_result(args.output.resolve(), rendered)
    print(rendered, end="")
    return 0 if payload["status"] == "passed" else 1


def _peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def _sha256(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise argparse.ArgumentTypeError("expected history snapshot hash must be lowercase SHA-256")
    return value


def _write_result(path: Path, rendered: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
