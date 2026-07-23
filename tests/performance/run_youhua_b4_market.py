from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import platform
import resource
import tempfile
import time
import tracemalloc
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import run_v15_market_data as scalar_runner

import trader.infra.market_data.merge as merge_module
from trader.domain.market.models import CanonicalMarketSnapshot
from trader.infra.market_data.columnar import ColumnarQuoteBatch, market_changes
from trader.infra.market_data.columnar_merge import (
    CompleteRealtimeNormalization,
    try_merge_complete_realtime,
    try_normalize_complete_realtime_rows,
)
from trader.infra.market_data.merge import merge_market_observations, observation_from_quote, snapshot_payload_hash
from trader.infra.settings import load_runtime_settings


def _measure(config_path: Path, fixture_path: Path) -> dict[str, Any]:
    settings = load_runtime_settings(config_path)
    _manifest, template, fixture_hash = scalar_runner._load_fixture(fixture_path)
    rows = scalar_runner._expand_rows(settings.performance_budgets.workload.market_rows, template)
    observed_at = scalar_runner.datetime.fromisoformat(scalar_runner._text(template, "observed_at"))
    east_quotes = scalar_runner._normalize(rows, "eastmoney", observed_at, 1.0)
    sina_quotes = scalar_runner._normalize(
        rows,
        "sina",
        observed_at,
        scalar_runner._number(template, "sina_price_multiplier"),
    )
    observations = (
        *(observation_from_quote(quote, source="eastmoney", observed_at=observed_at) for quote in east_quotes),
        *(observation_from_quote(quote, source="sina", observed_at=observed_at) for quote in sina_quotes),
    )
    scalar_samples, columnar_samples, scalar_wall, columnar_wall, output_hash = _relative_pipeline_samples(
        rows,
        observed_at,
        sina_price_multiplier=scalar_runner._number(template, "sina_price_multiplier"),
        warmup=settings.performance_budgets.rounds.warmup,
        measurement=settings.performance_budgets.rounds.measurement,
    )
    scalar_p95 = _nearest_rank(scalar_samples, 0.95)
    columnar_p95 = _nearest_rank(columnar_samples, 0.95)
    improvement = (scalar_p95 - columnar_p95) / max(scalar_p95, 0.000001) * 100.0

    absolute = scalar_runner._run(settings, template, fixture_hash)
    base_snapshot = merge_market_observations(observations, observed_at=observed_at)
    tencent_quotes = scalar_runner._normalize(
        rows[: settings.performance_budgets.workload.candidate_rows],
        "tencent",
        observed_at,
        scalar_runner._number(template, "tencent_price_multiplier"),
    )
    targeted_observations = tuple(
        observation_from_quote(quote, source="tencent", observed_at=observed_at) for quote in tencent_quotes
    )
    targeted_snapshot = merge_market_observations(
        (*observations, *targeted_observations),
        observed_at=observed_at,
        targeted_codes=tuple(quote.code for quote in tencent_quotes),
    )
    base_batch = ColumnarQuoteBatch.from_snapshot(
        base_snapshot,
        config_version=settings.config_version,
        schema_version=settings.market_data.cache_policy.policy_version,
    )
    targeted_batch = ColumnarQuoteBatch.from_snapshot(
        targeted_snapshot,
        config_version=settings.config_version,
        schema_version=settings.market_data.cache_policy.policy_version,
    )
    memory = _memory_probe(base_batch, targeted_batch, int(absolute["cache"]["estimated_bytes"]))
    memory_budget = settings.performance_budgets.memory
    passed = (
        bool(absolute["passed"])
        and improvement >= 20.0
        and memory["allocation_growth_percent"] <= memory_budget.growth_percent
        and memory["cache_logical_bytes"] <= memory_budget.cache_logical_bytes
        and memory["process_peak_rss_bytes"] <= memory_budget.process_peak_rss_bytes
    )
    return {
        "schema_version": "youhua-b4-market-acceptance-v1",
        "passed": passed,
        "fixture_sha256": fixture_hash,
        "identity": {
            "base_commit": _git_head(config_path.parents[2]),
            "b_owned_tree_sha256": _tree_digest(config_path.parents[2] / "src" / "trader" / "infra" / "market_data"),
            "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu": platform.processor() or "unknown",
        },
        "workload": {
            "market_rows": len(east_quotes),
            "candidate_rows": len(tencent_quotes),
            "sources": ["eastmoney", "sina"],
            "ticks": 100,
            "network_calls": 0,
        },
        "relative_normalize_and_merge": {
            "scalar_p95_ms": round(scalar_p95, 3),
            "columnar_p95_ms": round(columnar_p95, 3),
            "clock": "process_cpu",
            "improvement_percent": round(improvement, 3),
            "required_improvement_percent": 20.0,
            "scalar_samples_ms": [round(value, 3) for value in scalar_samples],
            "columnar_samples_ms": [round(value, 3) for value in columnar_samples],
            "wall_p95_ms": {
                "scalar": round(_nearest_rank(scalar_wall, 0.95), 3),
                "columnar": round(_nearest_rank(columnar_wall, 0.95), 3),
            },
            "business_snapshot_sha256": output_hash,
            "passed": improvement >= 20.0,
        },
        "absolute_market": {
            "metrics": absolute["metrics"],
            "canonical_snapshot_sha256": absolute["canonical_snapshot_sha256"],
            "passed": absolute["passed"],
        },
        "memory": {
            **memory,
            "allocation_growth_budget_percent": memory_budget.growth_percent,
            "cache_logical_budget_bytes": memory_budget.cache_logical_bytes,
            "process_peak_rss_budget_bytes": memory_budget.process_peak_rss_bytes,
        },
    }


def _relative_pipeline_samples(
    rows: Sequence[Mapping[str, object]],
    observed_at: datetime,
    *,
    sina_price_multiplier: float,
    warmup: int,
    measurement: int,
) -> tuple[list[float], list[float], list[float], list[float], str]:
    columnar = merge_module.try_merge_complete_realtime
    scalar_samples: list[float] = []
    columnar_samples: list[float] = []
    scalar_wall: list[float] = []
    columnar_wall: list[float] = []
    output_hash = ""
    try:
        for index in range(warmup + measurement):
            if index % 2 == 0:
                scalar, scalar_cpu_ms, scalar_wall_ms = _timed_normalize_and_merge(
                    rows,
                    observed_at,
                    sina_price_multiplier=sina_price_multiplier,
                    use_columnar=False,
                )
                optimized, columnar_cpu_ms, columnar_wall_ms = _timed_normalize_and_merge(
                    rows,
                    observed_at,
                    sina_price_multiplier=sina_price_multiplier,
                    use_columnar=True,
                )
            else:
                optimized, columnar_cpu_ms, columnar_wall_ms = _timed_normalize_and_merge(
                    rows,
                    observed_at,
                    sina_price_multiplier=sina_price_multiplier,
                    use_columnar=True,
                )
                scalar, scalar_cpu_ms, scalar_wall_ms = _timed_normalize_and_merge(
                    rows,
                    observed_at,
                    sina_price_multiplier=sina_price_multiplier,
                    use_columnar=False,
                )
            if scalar != optimized:
                raise ValueError("scalar and columnar market snapshots differ")
            output_hash = snapshot_payload_hash(optimized)
            if index >= warmup:
                scalar_samples.append(scalar_cpu_ms)
                columnar_samples.append(columnar_cpu_ms)
                scalar_wall.append(scalar_wall_ms)
                columnar_wall.append(columnar_wall_ms)
    finally:
        merge_module.try_merge_complete_realtime = columnar
    return scalar_samples, columnar_samples, scalar_wall, columnar_wall, output_hash


def _timed_normalize_and_merge(
    rows: Sequence[Mapping[str, object]],
    observed_at: datetime,
    *,
    sina_price_multiplier: float,
    use_columnar: bool,
) -> tuple[CanonicalMarketSnapshot, float, float]:
    merge_module.try_merge_complete_realtime = (
        try_merge_complete_realtime if use_columnar else (lambda _observations: None)
    )
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    if use_columnar:
        east_observations = try_normalize_complete_realtime_rows(
            rows,
            CompleteRealtimeNormalization(
                source="eastmoney",
                observed_at=observed_at,
                source_time=observed_at,
                received_at=observed_at,
                data_version="eastmoney-fixture-v1",
            ),
        )
        sina_observations = try_normalize_complete_realtime_rows(
            rows,
            CompleteRealtimeNormalization(
                source="sina",
                observed_at=observed_at,
                source_time=observed_at,
                received_at=observed_at,
                data_version="sina-fixture-v1",
                price_multiplier=sina_price_multiplier,
            ),
        )
        if east_observations is None or sina_observations is None:
            raise ValueError("columnar normalization rejected the fixed complete fixture")
        observations = (*east_observations, *sina_observations)
    else:
        east_quotes = scalar_runner._normalize(rows, "eastmoney", observed_at, 1.0)
        sina_quotes = scalar_runner._normalize(rows, "sina", observed_at, sina_price_multiplier)
        observations = (
            *(observation_from_quote(quote, source="eastmoney", observed_at=observed_at) for quote in east_quotes),
            *(observation_from_quote(quote, source="sina", observed_at=observed_at) for quote in sina_quotes),
        )
    snapshot = merge_market_observations(observations, observed_at=observed_at)
    cpu_ms = (time.process_time() - cpu_started) * 1000.0
    wall_ms = (time.perf_counter() - wall_started) * 1000.0
    return snapshot, cpu_ms, wall_ms


def _memory_probe(
    first: ColumnarQuoteBatch,
    second: ColumnarQuoteBatch,
    cache_estimated_bytes: int,
) -> dict[str, int | float | str | None]:
    gc.collect()
    tracemalloc.start()
    retained_at_tick_10 = 0
    last_changes = None
    for tick in range(100):
        previous, current = (first, second) if tick % 2 == 0 else (second, first)
        last_changes = market_changes(previous, current)
        if tick == 9:
            gc.collect()
            retained_at_tick_10 = tracemalloc.get_traced_memory()[0]
    gc.collect()
    traced_current, traced_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    if last_changes is None:
        raise ValueError("100 tick memory probe did not produce a change set")
    growth = max(0.0, (traced_current - retained_at_tick_10) / max(retained_at_tick_10, 1) * 100.0)
    polars_bytes = first.frame.estimated_size() + second.frame.estimated_size()
    return {
        "ticks": 100,
        "allocation_growth_percent": round(growth, 6),
        "cache_logical_bytes": cache_estimated_bytes + polars_bytes,
        "market_cache_estimated_bytes": cache_estimated_bytes,
        "polars_estimated_bytes": polars_bytes,
        "python_traced_bytes": traced_current,
        "python_traced_peak_bytes": traced_peak,
        "process_rss_bytes": _rss_bytes(),
        "process_peak_rss_bytes": _peak_rss_bytes(),
        "process_uss_bytes": _uss_bytes(),
        "last_dirty_codes": len(last_changes.dirty_codes),
        "transient_peak_reason": "two retained 5500-row columnar epochs plus alternating full/targeted dirty projection",
    }


def _nearest_rank(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("performance samples must not be empty")
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(probability * len(ordered) + 0.999999) - 1))
    return ordered[index]


def _rss_bytes() -> int:
    statm = Path("/proc/self/statm").read_text(encoding="utf-8").split()
    return int(statm[1]) * os.sysconf("SC_PAGE_SIZE")


def _peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value * 1024) if platform.system() == "Linux" else int(value)


def _uss_bytes() -> int | None:
    try:
        fields = {
            line.partition(":")[0]: int(line.partition(":")[2].strip().split()[0]) * 1024
            for line in Path("/proc/self/smaps_rollup").read_text(encoding="utf-8").splitlines()
            if ":" in line and line.partition(":")[2].strip()
        }
    except (OSError, ValueError):
        return None
    return fields.get("Private_Clean", 0) + fields.get("Private_Dirty", 0)


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*.py") if "__pycache__" not in item.parts):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _git_head(root: Path) -> str:
    head = root / ".git" / "HEAD"
    raw = head.read_text(encoding="utf-8").strip()
    if raw.startswith("ref: "):
        return (root / ".git" / raw[5:]).read_text(encoding="utf-8").strip()
    return raw


def _atomic_write(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the fixed youhua B4 market acceptance suite")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        report = _measure(arguments.config.resolve(), arguments.fixture.resolve())
    except Exception as exc:
        report = {
            "schema_version": "youhua-b4-market-acceptance-v1",
            "passed": False,
            "error": type(exc).__name__,
            "message": str(exc)[:300],
        }
        _atomic_write(arguments.output.resolve(), report)
        return 2
    _atomic_write(arguments.output.resolve(), report)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
