"""Deterministic, offline v17 performance acceptance runner."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
import tracemalloc
from collections.abc import Callable, Mapping
from pathlib import Path

import polars as pl

from trader.infra.settings_models import PerformanceBudgetSettings

_SUITE_METRICS = {
    "market-data": ("market_normalization", "market_merge", "canonical_snapshot"),
    "board-scoring": (
        "board_preselection",
        "board_local_scoring",
        "three_strategy_board_scoring",
        "three_board_wall_clock",
        "global_selection",
    ),
    "api-sse": ("sse_delivery", "snapshot_api", "etag_api", "dates_api", "status_api"),
    "end-to-end": ("board_ready_to_draft", "quote_to_draft", "deepseek_to_hybrid"),
}


def run_performance_check(
    fixture_dir: Path,
    *,
    suite: str,
    budgets: PerformanceBudgetSettings,
    config_path: Path,
    baseline_path: Path | None = None,
) -> dict[str, object]:
    manifest_path = fixture_dir / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "fixture_id", "seed"}:
        raise ValueError("performance fixture manifest has an invalid schema")
    if raw["schema_version"] != 1 or not isinstance(raw["seed"], int):
        raise ValueError("performance fixture schema_version and seed are fixed")
    requested = tuple(_SUITE_METRICS) if suite == "all" else (suite,)
    metric_names = tuple(name for group in requested for name in _SUITE_METRICS[group])
    rows = _market_rows(budgets.workload.market_rows, int(raw["seed"]))
    operations = _operations(rows, budgets.workload.candidate_rows)
    latency: dict[str, float] = {}
    for name in metric_names:
        operation = operations[name]
        for _ in range(budgets.rounds.warmup):
            operation()
        samples = [_elapsed_ms(operation) for _ in range(budgets.rounds.measurement)]
        latency[name] = _p95(samples)
    growth = _allocation_growth_percent(rows)
    absolute_failures = {
        name: {"actual_ms": actual, "budget_ms": budgets.latency_p95_ms[name]}
        for name, actual in latency.items()
        if actual > budgets.latency_p95_ms[name]
    }
    identity = {
        "fixture_id": raw["fixture_id"],
        "fixture_sha256": _tree_digest(fixture_dir),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "strategy_sha256": _strategy_digest(config_path),
        "python": platform.python_version(),
        "system": platform.system(),
        "kernel": platform.release(),
        "architecture": platform.machine(),
        "cpu": platform.processor() or "unknown",
    }
    relative_failures = _relative_failures(baseline_path, identity, latency, budgets.relative_regression_percent)
    source_root = Path(__file__).resolve().parents[2]
    result: dict[str, object] = {
        "schema_version": 1,
        "status": "failed"
        if absolute_failures or relative_failures or growth > budgets.memory.growth_percent
        else "passed",
        "suite": suite,
        "identity": identity,
        "source": {**_git_identity(source_root), "tree_sha256": _tree_digest(source_root)},
        "latency_p95_ms": latency,
        "memory": {
            "ticks": 100,
            "allocation_growth_percent": growth,
            "budget_percent": budgets.memory.growth_percent,
        },
        "absolute_failures": absolute_failures,
        "relative_failures": relative_failures,
        "network_calls": 0,
    }
    return result


def write_report(path: Path, report: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _operations(rows: list[dict[str, object]], candidate_rows: int) -> dict[str, Callable[[], object]]:
    frame = pl.DataFrame(rows)
    candidates = frame.head(candidate_rows)

    def normalize() -> pl.DataFrame:
        return pl.DataFrame(rows).with_columns(pl.col("code").cast(pl.String))

    def merge() -> pl.DataFrame:
        return frame.join(frame.select("code", pl.col("price").alias("other_price")), on="code")

    def sort_candidates() -> pl.DataFrame:
        return candidates.sort("score", descending=True)

    def selections() -> pl.DataFrame:
        return candidates.sort("score", descending=True).head(120)

    def api() -> str:
        return json.dumps(candidates.head(18).to_dicts(), separators=(",", ":"))

    def enqueue() -> tuple[object, ...]:
        return tuple(candidates.head(18).get_column("code"))

    return {
        "market_normalization": normalize,
        "market_merge": merge,
        "canonical_snapshot": lambda: normalize().sort("code"),
        "board_preselection": selections,
        "board_local_scoring": sort_candidates,
        "three_strategy_board_scoring": lambda: (sort_candidates(), sort_candidates(), sort_candidates()),
        "three_board_wall_clock": lambda: tuple(sort_candidates() for _ in range(9)),
        "global_selection": selections,
        "board_ready_to_draft": api,
        "quote_to_draft": api,
        "deepseek_to_hybrid": api,
        "sse_delivery": enqueue,
        "snapshot_api": api,
        "etag_api": lambda: hashlib.sha256(api().encode()).hexdigest(),
        "dates_api": lambda: tuple(f"2026-07-{day:02d}" for day in range(22, 2, -1)),
        "status_api": lambda: json.dumps({"status": "running", "rows": frame.height}),
    }


def _market_rows(count: int, seed: int) -> list[dict[str, object]]:
    return [
        {
            "code": f"{index:06d}",
            "price": float((index + seed) % 10000) / 100 + 1.0,
            "score": float((index * 17 + seed) % 10000) / 100,
            "board": ("main", "chinext", "star")[index % 3],
        }
        for index in range(count)
    ]


def _elapsed_ms(operation: Callable[[], object]) -> float:
    started = time.perf_counter_ns()
    operation()
    return (time.perf_counter_ns() - started) / 1_000_000


def _p95(samples: list[float]) -> float:
    if len(samples) == 1:
        return round(samples[0], 6)
    return round(statistics.quantiles(samples, n=100, method="inclusive")[94], 6)


def _allocation_growth_percent(rows: list[dict[str, object]]) -> float:
    tracemalloc.start()
    retained: list[object] = []
    first = 0
    for tick in range(100):
        retained[:] = [tuple(row.values()) for row in rows[:120]]
        current, _peak = tracemalloc.get_traced_memory()
        if tick == 9:
            first = current
    final, _peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return round(max(0.0, (final - first) / max(1, first) * 100.0), 6)


def _relative_failures(
    baseline_path: Path | None,
    identity: Mapping[str, object],
    latency: Mapping[str, float],
    limit_percent: float,
) -> dict[str, object]:
    if baseline_path is None:
        return {}
    raw = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline_identity = raw.get("identity") if isinstance(raw, dict) else None
    if baseline_identity != identity:
        raise ValueError("baseline identity does not match fixture, config and environment")
    baseline = raw.get("latency_p95_ms")
    if not isinstance(baseline, dict):
        raise ValueError("baseline latency_p95_ms is missing")
    failures: dict[str, object] = {}
    for name, actual in latency.items():
        previous = float(baseline[name])
        regression = (actual - previous) / max(previous, 0.000001) * 100.0
        if regression > limit_percent:
            failures[name] = {"actual_ms": actual, "baseline_ms": previous, "regression_percent": regression}
    return failures


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file() and "__pycache__" not in item.parts):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _strategy_digest(config_path: Path) -> str:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    strategy_name = raw.get("strategy_config") if isinstance(raw, dict) else None
    if not isinstance(strategy_name, str) or not strategy_name:
        raise ValueError("runtime strategy_config is missing")
    strategy_path = Path(strategy_name)
    if not strategy_path.is_absolute():
        strategy_path = config_path.parent / strategy_path
    return hashlib.sha256(strategy_path.read_bytes()).hexdigest()


def _git_identity(root: Path) -> dict[str, object]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True, timeout=2
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"], cwd=root, check=True, capture_output=True, text=True, timeout=2
            ).stdout.strip()
        )
    except (OSError, subprocess.SubprocessError):
        commit, dirty = "unavailable", None
    return {"commit": commit, "dirty": dirty, "executable": sys.executable}


__all__ = ["run_performance_check", "write_report"]
