from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from trader.application.cache import build_cache_identity, canonical_json_bytes
from trader.application.workers import BoundedExecutor
from trader.domain.market.models import MarketQuote
from trader.infra.cache import BoundedLruCache
from trader.infra.market_data.merge import (
    merge_market_observations,
    observation_from_quote,
    snapshot_payload_hash,
)
from trader.infra.market_data.normalize import MarketQuoteInput, build_market_quote
from trader.infra.market_data.observations import SourceObservation
from trader.infra.settings import load_runtime_settings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the fixed v15 market-data performance suite")
    parser.add_argument("--config", required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args(argv)
    output = Path(arguments.output).expanduser()
    report: dict[str, object]
    try:
        config_path = _absolute_existing_file(arguments.config, "config")
        fixture_path = _absolute_existing_directory(arguments.fixture, "fixture")
        if not output.is_absolute():
            raise ValueError("output must be an absolute path")
        settings = load_runtime_settings(config_path)
        manifest, template, fixture_hash = _load_fixture(fixture_path)
        _validate_workload(settings, manifest)
        report = _run(settings, template, fixture_hash)
        exit_code = 0 if report["passed"] is True else 1
    except Exception as exc:
        report = {
            "schema_version": "v15-performance-report-v1",
            "passed": False,
            "valid": False,
            "error": {"code": type(exc).__name__, "message": str(exc)[:500]},
        }
        exit_code = 2
    _atomic_write(output, report)
    return exit_code


def _run(settings: Any, template: Mapping[str, object], fixture_hash: str) -> dict[str, object]:
    workload = settings.performance_budgets.workload
    rounds = settings.performance_budgets.rounds
    observed_at = datetime.fromisoformat(_text(template, "observed_at"))
    rows = _expand_rows(workload.market_rows, template)
    east_quotes = _normalize(rows, "eastmoney", observed_at, 1.0)
    sina_quotes = _normalize(rows, "sina", observed_at, _number(template, "sina_price_multiplier"))
    east_observations = tuple(
        observation_from_quote(quote, source="eastmoney", observed_at=observed_at) for quote in east_quotes
    )
    sina_observations = tuple(
        observation_from_quote(quote, source="sina", observed_at=observed_at) for quote in sina_quotes
    )
    candidate_rows = rows[: workload.candidate_rows]
    tencent_quotes = _normalize(
        candidate_rows,
        "tencent",
        observed_at,
        _number(template, "tencent_price_multiplier"),
    )
    tencent_observations = tuple(
        observation_from_quote(quote, source="tencent", observed_at=observed_at) for quote in tencent_quotes
    )
    targeted_codes = tuple(quote.code for quote in tencent_quotes)

    normalization_samples: list[float] = []
    merge_samples: list[float] = []
    canonical_samples: list[float] = []
    hashes: set[str] = set()
    total_rounds = rounds.warmup + rounds.measurement
    for index in range(total_rounds):
        started = time.perf_counter()
        normalized = _normalize(rows, "eastmoney", observed_at, 1.0)
        normalization_ms = _elapsed_ms(started)
        if len(normalized) != workload.market_rows:
            raise ValueError("normalization did not produce the fixed market row count")

        started = time.perf_counter()
        merged = merge_market_observations(
            (*east_observations, *sina_observations),
            observed_at=observed_at,
        )
        merge_ms = _elapsed_ms(started)

        started = time.perf_counter()
        canonical = merge_market_observations(
            (*east_observations, *sina_observations, *tencent_observations),
            observed_at=observed_at,
            targeted_codes=targeted_codes,
        )
        canonical_hash = snapshot_payload_hash(canonical)
        canonical_ms = _elapsed_ms(started)
        if len(merged.quotes) != workload.market_rows or len(canonical.quotes) != workload.market_rows:
            raise ValueError("merge did not produce the fixed market row count")
        hashes.add(canonical_hash)
        if index >= rounds.warmup:
            normalization_samples.append(normalization_ms)
            merge_samples.append(merge_ms)
            canonical_samples.append(canonical_ms)
    if len(hashes) != 1:
        raise ValueError("canonical snapshot hash changed across identical rounds")

    metrics = {
        "market_normalization": _metric(normalization_samples),
        "market_merge": _metric(merge_samples),
        "canonical_snapshot": _metric(canonical_samples),
    }
    budgets = {
        name: float(settings.performance_budgets.latency_p95_ms[name])
        for name in ("market_normalization", "market_merge", "canonical_snapshot")
    }
    for name, metric in metrics.items():
        p95_ms = metric["p95_ms"]
        if not isinstance(p95_ms, (int, float)) or isinstance(p95_ms, bool):
            raise TypeError(f"{name} P95 must be numeric")
        metric["budget_p95_ms"] = budgets[name]
        metric["passed"] = float(p95_ms) <= budgets[name]
    cache_report = _cache_probe(
        settings,
        observed_at,
        east_observations,
        tencent_observations,
    )
    slow_source = _slow_source_probe()
    passed = (
        all(metric["passed"] is True for metric in metrics.values())
        and cache_report["passed"] is True
        and slow_source["passed"] is True
    )
    budget_payload = _performance_budget_payload(settings)
    return {
        "schema_version": "v15-performance-report-v1",
        "valid": True,
        "passed": passed,
        "configuration_sha256": hashlib.sha256(canonical_json_bytes(budget_payload)).hexdigest(),
        "fixture_sha256": fixture_hash,
        "workload": budget_payload["workload"],
        "rounds": budget_payload["rounds"],
        "metrics": metrics,
        "cache": cache_report,
        "slow_source_isolation": slow_source,
        "canonical_snapshot_sha256": next(iter(hashes)),
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "system": platform.system(),
            "machine": platform.machine(),
            "logical_cpu_count": os.cpu_count(),
        },
        "error": None,
    }


def _cache_probe(
    settings: Any,
    observed_at: datetime,
    market_observations: Sequence[SourceObservation],
    candidate_observations: Sequence[SourceObservation],
) -> Mapping[str, object]:
    cache: BoundedLruCache[object] = BoundedLruCache(
        settings.market_data.cache_policy,
        cadence_seconds=settings.pipeline.cadence_seconds,
        wall_clock=lambda: observed_at,
    )
    rounds = settings.performance_budgets.rounds
    cold_samples: list[float] = []
    hot_samples: list[float] = []
    peak_entries = 0
    first_cold_states: tuple[str | None, str | None] | None = None
    first_hot_states: tuple[str | None, str | None] | None = None
    for index in range(rounds.warmup + rounds.measurement):
        market_identity = build_cache_identity(
            dataset="full_market_quotes",
            source="eastmoney",
            subject_key=f"market-{index}",
            request={"universe": "ashare", "fields": ["realtime_quote"], "sample": index},
            trade_date=observed_at.date().isoformat(),
            phase="today_main",
            source_contract_version="performance-v1",
            config_version=settings.config_version,
            schema_version="market_snapshot_v15",
        )
        candidate_identity = build_cache_identity(
            dataset="candidate_quotes",
            source="tencent",
            subject_key=f"candidate-{index}",
            request={"codes": [item.subject_key for item in candidate_observations], "sample": index},
            trade_date=observed_at.date().isoformat(),
            phase="today_main",
            source_contract_version="performance-v1",
            config_version=settings.config_version,
            schema_version="market_snapshot_v15",
        )
        started = time.perf_counter()
        cold_market = cache.get(market_identity)
        cold_candidate = cache.get(candidate_identity)
        market_stored = cache.put(
            market_identity,
            tuple(market_observations),
            data_version=f"eastmoney-fixture-v{index}",
            source_time=observed_at,
        )
        candidate_stored = cache.put(
            candidate_identity,
            tuple(candidate_observations),
            data_version=f"tencent-fixture-v{index}",
            source_time=observed_at,
        )
        cold_ms = _elapsed_ms(started)
        started = time.perf_counter()
        hot_market = cache.get(market_identity)
        hot_candidate = cache.get(candidate_identity)
        hot_ms = _elapsed_ms(started)
        if index == 0:
            first_cold_states = (
                None if cold_market is None else cold_market.state,
                None if cold_candidate is None else cold_candidate.state,
            )
            first_hot_states = (
                None if hot_market is None else hot_market.state,
                None if hot_candidate is None else hot_candidate.state,
            )
        if not market_stored or not candidate_stored:
            raise ValueError("cache could not store the fixed v15 workload")
        if index >= rounds.warmup:
            cold_samples.append(cold_ms)
            hot_samples.append(hot_ms)
        current = cache.status()
        peak_entries = max(
            peak_entries,
            int(current["full_market_quotes"]["eastmoney"]["entries"])
            + int(current["candidate_quotes"]["tencent"]["entries"]),
        )
    status = cache.status()
    market_status = status["full_market_quotes"]["eastmoney"]
    candidate_status = status["candidate_quotes"]["tencent"]
    total_hits = int(market_status["hit"]) + int(candidate_status["hit"])
    total_misses = int(market_status["miss"]) + int(candidate_status["miss"])
    total_capacity = int(market_status["capacity"]) + int(candidate_status["capacity"])
    cache.stop()
    return {
        "cold_states": first_cold_states,
        "hot_states": first_hot_states,
        "cold_path": _metric(cold_samples),
        "hot_path": _metric(hot_samples),
        "workload_rows": {
            "full_market": len(market_observations),
            "candidates": len(candidate_observations),
        },
        "peak_entries": peak_entries,
        "estimated_bytes": int(market_status["estimated_bytes"]) + int(candidate_status["estimated_bytes"]),
        "hit_rate": total_hits / (total_hits + total_misses),
        "stats": status,
        "passed": (
            first_cold_states == (None, None)
            and first_hot_states == ("fresh", "fresh")
            and len(market_observations) == settings.performance_budgets.workload.market_rows
            and len(candidate_observations) == settings.performance_budgets.workload.candidate_rows
            and peak_entries <= total_capacity
        ),
    }


def _slow_source_probe() -> Mapping[str, object]:
    executor = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data-performance")
    release = threading.Event()
    started = threading.Event()

    def blocked() -> None:
        started.set()
        release.wait(2.0)

    executor.start()
    blocked_future = executor.submit(blocked)
    if blocked_future is None or not started.wait(1.0):
        executor.stop(wait=False, cancel_futures=True)
        return {"passed": False, "completed_fast_sources": 0}
    fast = [executor.submit(lambda value=index: value) for index in range(4)]
    completed = 0
    try:
        completed = sum(future is not None and future.result(timeout=1.0) >= 0 for future in fast)
    finally:
        release.set()
        blocked_future.result(timeout=1.0)
        executor.stop(wait=True, cancel_futures=True)
    return {"passed": completed == 4, "completed_fast_sources": completed}


def _load_fixture(path: Path) -> tuple[Mapping[str, object], Mapping[str, object], str]:
    manifest_path = path / "manifest.json"
    manifest = _json_object(manifest_path)
    if set(manifest) != {"schema_version", "generator_version", "market_rows", "candidate_rows", "files"}:
        raise ValueError("fixture manifest keys are invalid")
    if manifest.get("schema_version") != "v15-performance-fixture-v1":
        raise ValueError("fixture manifest schema is invalid")
    if manifest.get("generator_version") != "deterministic-code-expansion-v1":
        raise ValueError("fixture generator version is invalid")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != {"quotes.template.json"}:
        raise ValueError("fixture manifest file set is invalid")
    template_path = path / "quotes.template.json"
    payload = template_path.read_bytes()
    actual_hash = hashlib.sha256(payload).hexdigest()
    if files["quotes.template.json"] != actual_hash:
        raise ValueError("fixture file hash mismatch")
    fixture_hash = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    template = _json_object(template_path)
    if set(template) != {"schema_version", "observed_at", "base", "sina_price_multiplier", "tencent_price_multiplier"}:
        raise ValueError("quote template keys are invalid")
    if template.get("schema_version") != "v15-sanitized-quotes-v1":
        raise ValueError("quote template schema is invalid")
    return manifest, template, fixture_hash


def _validate_workload(settings: Any, manifest: Mapping[str, object]) -> None:
    if manifest.get("market_rows") != settings.performance_budgets.workload.market_rows:
        raise ValueError("fixture market_rows does not match configured workload")
    if manifest.get("candidate_rows") != settings.performance_budgets.workload.candidate_rows:
        raise ValueError("fixture candidate_rows does not match configured workload")


def _performance_budget_payload(settings: Any) -> dict[str, object]:
    budgets = settings.performance_budgets
    return {
        "schema_version": budgets.schema_version,
        "workload": {
            "market_rows": budgets.workload.market_rows,
            "candidate_rows": budgets.workload.candidate_rows,
        },
        "rounds": {
            "warmup": budgets.rounds.warmup,
            "measurement": budgets.rounds.measurement,
        },
        "latency_p95_ms": dict(budgets.latency_p95_ms),
        "data_age_p95_seconds": dict(budgets.data_age_p95_seconds),
        "memory": {
            "cache_logical_bytes": budgets.memory.cache_logical_bytes,
            "process_peak_rss_bytes": budgets.memory.process_peak_rss_bytes,
            "growth_percent": budgets.memory.growth_percent,
        },
        "relative_regression_percent": budgets.relative_regression_percent,
    }


def _expand_rows(count: int, template: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    base = template.get("base")
    if not isinstance(base, dict):
        raise ValueError("quote template base must be an object")
    expected_base_keys = {
        "name_prefix",
        "price",
        "previous_close",
        "open_price",
        "high",
        "low",
        "pct_change",
        "change_5m",
        "speed",
        "volume_ratio",
        "turnover_rate",
        "amount",
        "amplitude",
        "market_cap",
        "industry_count",
    }
    if set(base) != expected_base_keys:
        raise ValueError("quote template base keys are invalid")
    industry_count_value = _number(base, "industry_count")
    if industry_count_value <= 0 or not industry_count_value.is_integer():
        raise ValueError("quote template industry_count must be a positive integer")
    industry_count = int(industry_count_value)
    rows: list[dict[str, object]] = []
    for index in range(count):
        code = f"{index + 100000:06d}"
        rows.append(
            {
                "code": code,
                "name": f"{_text(base, 'name_prefix')}{index:04d}",
                "price": _number(base, "price") + (index % 100) * 0.001,
                "previous_close": _number(base, "previous_close"),
                "open_price": _number(base, "open_price"),
                "high": _number(base, "high") + (index % 100) * 0.001,
                "low": _number(base, "low"),
                "pct_change": _number(base, "pct_change"),
                "change_5m": _number(base, "change_5m"),
                "speed": _number(base, "speed"),
                "volume_ratio": _number(base, "volume_ratio"),
                "turnover_rate": _number(base, "turnover_rate"),
                "amount": _number(base, "amount") + index,
                "amplitude": _number(base, "amplitude"),
                "market_cap": _number(base, "market_cap") + index * 1000,
                "industry": f"行业{index % industry_count:02d}",
            }
        )
    return tuple(rows)


def _normalize(
    rows: Sequence[Mapping[str, object]],
    source: str,
    observed_at: datetime,
    price_multiplier: float,
) -> tuple[MarketQuote, ...]:
    result: list[MarketQuote] = []
    for row in rows:
        price = _number(row, "price") * price_multiplier
        result.append(
            build_market_quote(
                MarketQuoteInput(
                    code=str(row["code"]),
                    name=str(row["name"]),
                    price=price,
                    previous_close=_number(row, "previous_close"),
                    open_price=_number(row, "open_price"),
                    high=max(price, _number(row, "high")),
                    low=_number(row, "low"),
                    pct_change=_number(row, "pct_change"),
                    change_5m=_number(row, "change_5m"),
                    speed=_number(row, "speed"),
                    volume_ratio=_number(row, "volume_ratio"),
                    turnover_rate=_number(row, "turnover_rate"),
                    amount=_number(row, "amount"),
                    amplitude=_number(row, "amplitude"),
                    market_cap=_number(row, "market_cap"),
                    industry=str(row["industry"]),
                    source=source,
                    source_time=observed_at,
                    received_time=observed_at,
                    data_version=f"{source}-fixture-v1",
                )
            )
        )
    return tuple(result)


def _metric(samples: Sequence[float]) -> dict[str, object]:
    if not samples:
        raise ValueError("performance metric requires measurement samples")
    ordered = sorted(samples)
    return {
        "samples": len(ordered),
        "p50_ms": round(_nearest_rank(ordered, 0.50), 3),
        "p95_ms": round(_nearest_rank(ordered, 0.95), 3),
        "max_ms": round(ordered[-1], 3),
    }


def _nearest_rank(ordered: Sequence[float], quantile: float) -> float:
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _absolute_existing_file(raw: str, label: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute() or not path.is_file():
        raise ValueError(f"{label} must be an existing absolute file")
    return path.resolve()


def _absolute_existing_directory(raw: str, label: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise ValueError(f"{label} must be an existing absolute directory")
    return path.resolve()


def _json_object(path: Path) -> Mapping[str, object]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return raw


def _text(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _number(raw: Mapping[str, object], key: str) -> float:
    value = raw.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
        raise ValueError(f"{key} must be a finite number")
    return float(value)


def _atomic_write(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
