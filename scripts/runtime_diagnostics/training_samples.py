"""Five alternating sample-layout and columnar-read experiments on external fixed fixtures."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path
from importlib.metadata import version
import hashlib
import platform
from typing import TypedDict, cast

from trader.infra.settings import LoadRuntimeSettings

from .database_workspace import PrepareDatabaseProbeDirectory, ProjectDatabaseProbeLocation
from .training_read_comparison import PrepareTrainingHistory


class _Case(TypedDict, total=False):
    write_seconds: float
    read_seconds: float
    seconds: float
    numeric_hash: str
    rows: int


class _Run(TypedDict):
    cases: dict[str, _Case]
    peak_rss_bytes: int


def _run(argv: tuple[str, ...], cwd: Path, work: Path) -> _Run:
    completed = subprocess.run(
        argv, cwd=cwd, env={**os.environ, "PYTHONPATH": str(cwd / "src") + os.pathsep + str(cwd),
                           "PYTHONHASHSEED": "0", "TMPDIR": str(work)},
        capture_output=True, text=True, timeout=600, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("training_probe_worker_failed:" + completed.stderr[-2000:])
    return cast(_Run, json.loads(completed.stdout))


def _compare(
    runs: dict[str, list[_Run]], before: str, after: str, regression: float,
) -> list[dict[str, object]]:
    comparison = []
    for case, measurements in runs[before][0]["cases"].items():
        for metric in (name for name in measurements if name.endswith("seconds") and name != "export_seconds"):
            old = [float(cast(float, run["cases"][case][metric])) for run in runs[before]]  # type: ignore[literal-required]
            new = [float(cast(float, run["cases"][case][metric])) for run in runs[after]]  # type: ignore[literal-required]
            identical = all(
                left["cases"][case]["numeric_hash"] == right["cases"][case]["numeric_hash"]
                for left, right in zip(runs[before], runs[after], strict=True)
            )
            if not identical:
                raise RuntimeError(f"training_sample_numeric_mismatch:{case}")
            ratio = statistics.median(new) / statistics.median(old)
            comparison.append({
                "case": case, "metric": metric, "baseline_median_seconds": statistics.median(old),
                "candidate_median_seconds": statistics.median(new), "ratio": ratio,
                "wins": sum(b < a for a, b in zip(old, new, strict=True)),
                "within_regression_limit": ratio <= 1 + regression / 100, "numeric_identity": identical,
            })
    return comparison


def ProbeTrainingSamples(work: Path, baseline: Path, config: Path, *, size: int, sessions: int) -> dict[str, object]:
    project = Path(__file__).resolve().parents[2]
    baseline = baseline.resolve(strict=True)
    if baseline.is_relative_to(project) or not (baseline / "src/trader").is_dir():
        raise ValueError("training_sample_baseline_requires_external_checkout")
    if not 4 <= size <= 128 or not 64 <= sessions <= 750:
        raise ValueError("training_sample_probe_requires_4_to_128_codes_and_64_to_750_sessions")
    settings = LoadRuntimeSettings(config)
    location = PrepareDatabaseProbeDirectory(work, config)
    if location.memory_backed:
        raise ValueError("training_sample_disk_comparison_rejects_memory_filesystem")
    budget = settings.performance_budgets
    database_gate = budget.database.for_medium(location.storage_kind)
    storage_policy = settings.storage_policy.for_medium(location.storage_kind)
    if database_gate.evidence_status != "validated":
        raise ValueError(f"training_sample_{location.storage_kind}_performance_gate_is_unverified")
    rounds = budget.rounds.measurement
    runs: dict[str, list[_Run]] = {"baseline": [], "candidate": []}
    worker = Path(__file__).with_name("sample_comparison_worker.py")
    for number in range(rounds):
        for label in (("baseline", "candidate") if number % 2 == 0 else ("candidate", "baseline")):
            checkout = baseline if label == "baseline" else project
            runs[label].append(_run((
                sys.executable, str(worker), "--checkout", str(checkout),
                "--root", str(work / f"samples-{number}-{label}"), "--size", str(size), "--sessions", str(sessions),
            ), checkout, work))
    sample_comparison = _compare(runs, "baseline", "candidate", budget.relative_regression_percent)
    (work / "sample-results.json").write_text(json.dumps({"runs": runs, "comparison": sample_comparison}))
    history = PrepareTrainingHistory(work / "history-input", size=size, sessions=sessions)
    reads: dict[str, list[_Run]] = {"sqlite": [], "parquet": []}
    for number in range(rounds):
        for mode in (("sqlite", "parquet") if number % 2 == 0 else ("parquet", "sqlite")):
            reads[mode].append(_run((
                sys.executable, "-m", "scripts.runtime_diagnostics.training_read_comparison", "--history", str(history),
                "--output", str(work / f"parquet-{number}"), "--mode", mode,
            ), project, work))
    read_comparison = _compare(reads, "sqlite", "parquet", budget.relative_regression_percent)
    rss_limit = budget.memory.process_peak_rss_bytes
    all_runs = (*runs["baseline"], *runs["candidate"], *reads["sqlite"], *reads["parquet"])
    memory_passed = all(run["peak_rss_bytes"] <= rss_limit for run in all_runs)
    memory_growth = statistics.median(run["peak_rss_bytes"] for run in reads["parquet"]) / statistics.median(
        run["peak_rss_bytes"] for run in reads["sqlite"]
    )
    repeated = next(row for row in read_comparison if row["case"] == "repeat")
    parquet_eligible = (
        float(cast(float, repeated["ratio"])) <= 1 - database_gate.parquet_improvement_percent / 100
        and int(cast(int, repeated["wins"])) >= database_gate.minimum_wins
        and all(row["within_regression_limit"] for row in read_comparison)
        and memory_passed and memory_growth <= 1 + budget.memory.growth_percent / 100
    )
    sqlite_improvements = [
        row for row in sample_comparison
        if float(cast(float, row["ratio"])) <= 1 - database_gate.sqlite_improvement_percent / 100
        and int(cast(int, row["wins"])) >= database_gate.minimum_wins
    ]
    sqlite_passed = bool(sqlite_improvements) and memory_passed and all(
        row["within_regression_limit"] for row in sample_comparison
    )
    return {
        "schema_version": "training-samples-v1",
        "status": "passed" if sqlite_passed else "degraded",
        "summary": {
            "fixture": True, "config_hash": hashlib.sha256((project / "config/performance.json").read_bytes()).hexdigest(),
            "environment": {"python": platform.python_version(), "numpy": version("numpy"), "polars": version("polars"), "platform": platform.system()},
            "scope": "four-profile dual-head sample IO and history read/alignment; no model fitting or release",
            "rounds": rounds, "stocks": size, "sample_training_dates": sessions,
            "storage": ProjectDatabaseProbeLocation(location),
            "database_gate_medium": location.storage_kind,
            "database_gate_evidence_status": database_gate.evidence_status,
            "storage_policy": {
                "connection_timeout_seconds": storage_policy.connection_timeout_seconds,
                "read_window_rows": storage_policy.read_window_rows,
                "transaction_batch_rows": storage_policy.transaction_batch_rows,
                "checkpoint_mode": storage_policy.checkpoint_mode,
            },
            "policy": "fresh workers; alternating order; warm OS cache; unchanged algorithms; no network",
            "samples": sample_comparison, "sample_improvements": sqlite_improvements, "sqlite_performance_passed": sqlite_passed,
            "sample_timing_scope": "writes include deterministic feature fixture generation; reads include the same byte-hash verification",
            "sample_regressions": [row for row in sample_comparison if not row["within_regression_limit"]],
            "parquet": read_comparison, "parquet_eligible": parquet_eligible,
            "memory_passed": memory_passed, "parquet_rss_ratio": memory_growth,
            "memory_gate_scope": "isolated preparation worker RSS; full fitting scope and reservations unchanged",
            "sample_runs": runs, "read_runs": reads,
        },
    }


def Main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--baseline-checkout", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--size", type=int, default=64)
    parser.add_argument("--sessions", type=int, default=750)
    args = parser.parse_args()
    report = ProbeTrainingSamples(args.work_root, args.baseline_checkout, args.config, size=args.size, sessions=args.sessions)
    print(json.dumps(report))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(Main())
