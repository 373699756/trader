"""Measure current industry-cache operations on immutable replies and fresh external HDD databases."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from functools import partial
from pathlib import Path
from statistics import median
from typing import Literal, TypedDict, TypeVar

from scripts.core.paths import PROJECT_ROOT, ExternalPath
from scripts.infra.database.database_measurement import DatabaseMeasurement
from trader.download.infra.cninfo_industry import ParseCninfoIndustryChanges
from trader.download.infra.industry_query_index import IndustrySourceDocument, SQLiteIndustryQueryIndex
from trader.infra.atomic_files.json import AtomicWriteJson
from trader.infra.disk_probe import DiskProbe
from trader.infra.process_lock import ProcessLock
from trader.infra.regular_file import ReadRegularFile
from trader.infra.sqlite_observation import SqliteObservation
from trader.infra.sqlite_policy import SqliteIoPolicy

_MAX_BYTES = 2 * 1024 * 1024
_Result = TypeVar("_Result")
_SOURCE_FILES = (
    "scripts/download/history/industry_cache_measurement.py",
    "scripts/download/history/cli.py",
    "scripts/download/history/options.py",
    "scripts/download/history/commands.py",
    "scripts/download/history/history_industry_preparation.py",
    "scripts/infra/database/database_measurement.py",
    "src/trader/download/infra/industry_query_index.py",
    "src/trader/download/infra/current_industry_publication.py",
    "src/trader/download/infra/cninfo_industry.py",
    "src/trader/bootstrap.py",
    "src/trader/infra/sqlite_connection.py",
    "src/trader/infra/sqlite_observation.py",
    "src/trader/infra/sqlite_policy.py",
    "config/runtime.json",
    "config/sqlite.json",
)


@dataclass(frozen=True)
class IndustryCacheMeasurementRequest:
    evidence_root: Path
    codes: tuple[str, ...]
    work_root: Path
    budget_seconds: float = 300.0
    candidate: Literal["baseline", "batched", "compare"] = "baseline"


@dataclass(frozen=True)
class _Input:
    code: str
    receipt_path: Path
    receipt_sha256: str
    document: IndustrySourceDocument


class _CacheRound(TypedDict):
    round: int
    candidate: str
    ingest_wall_seconds: float
    ingest_operation_seconds: float
    ingest_unattributed_seconds: float
    round_wall_seconds: float
    database_bytes: int
    readback_exact: bool
    phases: list[dict[str, object]]


def _fingerprint(path: Path) -> str:
    return hashlib.sha256(ReadRegularFile(path, maximum_bytes=_MAX_BYTES)).hexdigest()


def _inputs(root: Path, codes: tuple[str, ...]) -> tuple[_Input, ...]:
    inputs = []
    for code in codes:
        receipt_path = root / ("single_" + code + ".receipt.json")
        content = ReadRegularFile(receipt_path, maximum_bytes=4096)
        payload: object = json.loads(content)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"codes", "encoding", "from_date", "http_status", "received_at", "sha256", "to_date", "url"}
            or payload["codes"] != [code] or payload["encoding"] != "post_query"
            or payload["http_status"] != 200 or not isinstance(payload["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", payload["sha256"]) is None
            or payload["url"] != "https://webapi.cninfo.com.cn/api/stock/p_stock2110"
            or any(not isinstance(payload[field], str) for field in ("from_date", "to_date", "received_at"))
        ):
            raise ValueError("industry_cache_measurement_receipt_invalid")
        raw = ReadRegularFile(root / (payload["sha256"] + ".blob"), maximum_bytes=_MAX_BYTES)
        if hashlib.sha256(raw).hexdigest() != payload["sha256"]:
            raise ValueError("industry_cache_measurement_original_changed")
        document = IndustrySourceDocument(
            "cninfo:p_stock2110:" + code, date.fromisoformat(payload["from_date"]), date.fromisoformat(payload["to_date"]),
            datetime.fromisoformat(payload["received_at"]), raw,
        )
        inputs.append(_Input(code, receipt_path, hashlib.sha256(content).hexdigest(), document))
    return tuple(inputs)


def _measure(
    root: Path,
    label: str,
    code: str | None,
    operation: Callable[[], _Result],
) -> tuple[_Result, dict[str, object]]:
    observation = SqliteObservation()
    with DatabaseMeasurement(root, sample_wal=False) as measurement, observation.bind():
        result = operation()
    activity = observation.snapshot()
    return result, {
        "phase": label, "code": code, **measurement.result,
        "observed_connection_attempts": activity.connection_attempts,
        "observed_statements_including_configuration": activity.statements,
    }


def _check(deadline: float, cancel_requested: Callable[[], bool]) -> None:
    if cancel_requested():
        raise RuntimeError("industry_cache_measurement_cancelled")
    if time.monotonic() >= deadline:
        raise TimeoutError("industry_cache_measurement_deadline")


def _number(metrics: dict[str, object], key: str) -> float:
    value = metrics[key]
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError("industry_cache_measurement_metric_invalid")
    return float(value)


def _count(metrics: dict[str, object], key: str) -> int:
    value = metrics[key]
    if type(value) is not int or value < 0:
        raise ValueError("industry_cache_measurement_metric_invalid")
    return value


def _measure_round(
    directory: Path,
    number: int,
    candidate: str,
    inputs: tuple[_Input, ...],
    sqlite_policy: SqliteIoPolicy,
    deadline: float,
    cancel_requested: Callable[[], bool],
) -> _CacheRound:
    directory.mkdir()
    index = SQLiteIndustryQueryIndex(directory / "industry.sqlite3", "cninfo_industry_queries", sqlite_policy=sqlite_policy)
    stages: list[dict[str, object]] = []
    started = time.perf_counter()
    _, measured = _measure(directory, "prepare", None, index.prepare)
    stages.append(measured)
    for item in inputs:
        _check(deadline, cancel_requested)
        previous, measured = _measure(
            directory, "read_miss", item.code,
            partial(index.read_source_document, item.document.request_key, item.document.received_at),
        )
        if previous is not None:
            raise ValueError("industry_cache_measurement_database_not_fresh")
        stages.append(measured)
        parsed, measured = _measure(directory, "parse", item.code, partial(
            ParseCninfoIndustryChanges, item.document.content, item.code,
            item.document.requested_from, item.document.requested_to,
            cancel_requested=lambda: cancel_requested() or time.monotonic() >= deadline,
        ))
        if parsed.response_sha256 != item.document.content_hash or not parsed.rows:
            raise ValueError("industry_cache_measurement_input_invalid")
        stages.append(measured)
        if candidate == "baseline":
            _, measured = _measure(directory, "save", item.code, partial(index.save_source_documents, (item.document,)))
            stages.append(measured)
    if candidate == "batched":
        _check(deadline, cancel_requested)
        _, measured = _measure(directory, "save", None, partial(index.save_source_documents, tuple(item.document for item in inputs)))
        stages.append(measured)
    ingest_seconds = time.perf_counter() - started
    operation_seconds = sum(_number(stage, "wall_seconds") for stage in stages)
    for item in inputs:
        _check(deadline, cancel_requested)
        restored, measured = _measure(
            directory, "read_back", item.code,
            partial(index.read_source_document, item.document.request_key, item.document.received_at),
        )
        if restored != item.document or restored.receipt_hash != item.document.receipt_hash:
            raise ValueError("industry_cache_measurement_readback_mismatch")
        stages.append(measured)
    result: _CacheRound = {
        "round": number, "candidate": candidate, "ingest_wall_seconds": ingest_seconds,
        "ingest_operation_seconds": operation_seconds,
        "ingest_unattributed_seconds": max(0.0, ingest_seconds - operation_seconds),
        "round_wall_seconds": time.perf_counter() - started,
        "database_bytes": index.path.stat().st_size, "readback_exact": True, "phases": stages,
    }
    AtomicWriteJson(directory / "measurement.json", result)
    return result


def _comparison(runs: list[_CacheRound]) -> dict[str, object]:
    baseline = [run for run in runs if run["candidate"] == "baseline"]
    candidate = [run for run in runs if run["candidate"] == "batched"]
    if not baseline or not candidate:
        return {"status": "not_compared"}
    baseline_median = median(run["ingest_operation_seconds"] for run in baseline)
    candidate_median = median(run["ingest_operation_seconds"] for run in candidate)
    wins = sum(new["ingest_operation_seconds"] < old["ingest_operation_seconds"] for old, new in zip(baseline, candidate, strict=True))
    return {
        "status": "shared_load_slice_only", "pairs": len(baseline),
        "baseline_median_seconds": baseline_median, "candidate_median_seconds": candidate_median,
        "candidate_to_baseline_ratio": candidate_median / baseline_median,
        "median_improvement_percent": (1 - candidate_median / baseline_median) * 100,
        "improved_pairs": wins, "pair_win_rate": wins / len(baseline),
        "all_readbacks_exact": all(run["readback_exact"] for run in runs),
        "initial_prepare_included": True,
        "duration_basis": "sum of exclusive ingest operation phase walls; observer setup, coordination and report writes excluded",
        "pair_results": [{
            "round": old["round"], "baseline_operation_seconds": old["ingest_operation_seconds"],
            "candidate_operation_seconds": new["ingest_operation_seconds"],
            "baseline_lifecycle_seconds": old["ingest_wall_seconds"], "candidate_lifecycle_seconds": new["ingest_wall_seconds"],
            "baseline_commits": sum(_count(stage, "commits") for stage in old["phases"]),
            "candidate_commits": sum(_count(stage, "commits") for stage in new["phases"]),
            "baseline_save_commits": sum(_count(stage, "commits") for stage in old["phases"] if stage["phase"] == "save"),
            "candidate_save_commits": sum(_count(stage, "commits") for stage in new["phases"] if stage["phase"] == "save"),
            "readbacks_exact": old["readback_exact"] and new["readback_exact"],
        } for old, new in zip(baseline, candidate, strict=True)],
        "pair_order": [[run["candidate"] for run in runs if run["round"] == number] for number in range(1, 6)],
    }


def MeasureIndustryCache(
    request: IndustryCacheMeasurementRequest,
    sqlite_policy: SqliteIoPolicy,
    *,
    cancel_requested: Callable[[], bool] = lambda: False,
) -> dict[str, object]:
    """Run five fresh-cache rounds or alternating pairs without making exclusive performance claims."""
    if (
        not 2 <= len(request.codes) <= 5 or request.codes != tuple(sorted(set(request.codes)))
        or any(re.fullmatch(r"[0-9]{6}", code) is None for code in request.codes)
        or not math.isfinite(request.budget_seconds) or not 1 <= request.budget_seconds <= 7200
        or request.candidate not in {"baseline", "batched", "compare"}
    ):
        raise ValueError("industry_cache_measurement_request_invalid")
    evidence = ExternalPath(request.evidence_root, "--cninfo-cache-evidence")
    root = ExternalPath(request.work_root, "--industry-work-root")
    if evidence is None or root is None or not evidence.is_dir() or root.exists() or root.is_symlink():
        raise ValueError("industry_cache_measurement_paths_invalid")
    profile = DiskProbe().inspect(root)
    if profile.kind != "hdd":
        raise ValueError("industry_cache_measurement_requires_physical_hdd")
    deadline = time.monotonic() + request.budget_seconds
    _check(deadline, cancel_requested)
    inputs = _inputs(evidence, request.codes)
    identities = {relative: _fingerprint(PROJECT_ROOT / relative) for relative in _SOURCE_FILES}
    root.mkdir(parents=True, exist_ok=False)
    load_before = os.getloadavg()
    rounds: list[_CacheRound] = []
    with ProcessLock(root / ".industry-cache-measurement.lock", label="industry-cache-measurement"):
        for number in range(1, 6):
            _check(deadline, cancel_requested)
            order: tuple[str, ...]
            if request.candidate == "compare":
                order = ("baseline", "batched") if number % 2 else ("batched", "baseline")
            else:
                order = (request.candidate,)
            for candidate in order:
                _check(deadline, cancel_requested)
                rounds.append(_measure_round(
                    root / ("round-" + str(number) + "-" + candidate), number, candidate, inputs,
                    sqlite_policy, deadline, cancel_requested,
                ))
    source_unchanged = identities == {relative: _fingerprint(PROJECT_ROOT / relative) for relative in _SOURCE_FILES}
    input_unchanged = all(
        _fingerprint(item.receipt_path) == item.receipt_sha256
        and _fingerprint(evidence / (item.document.content_hash + ".blob")) == item.document.content_hash
        for item in inputs
    )
    phases = ("prepare", "read_miss", "parse", "save", "read_back")
    summary = {
        "scope": "two_stock_industry_cache_slice" if len(inputs) == 2 else "bounded_industry_cache_slice",
        "production_eligible": False, "full_sync_performance_verified": False, "exclusive_performance_verified": False,
        "shared_development_and_user_sync_load": True, "network_requests": 0, "source_data_writes": 0,
        "rounds": 5, "stock_count": len(inputs), "source_unchanged": source_unchanged, "input_unchanged": input_unchanged,
        "disk": {"kind": profile.kind, "device": profile.device, "source": profile.source, "reason": profile.reason},
        "sqlite_policy": {
            "connection_timeout_seconds": sqlite_policy.connection_timeout_seconds,
            "read_window_rows": sqlite_policy.read_window_rows, "transaction_batch_rows": sqlite_policy.transaction_batch_rows,
            "checkpoint_mode": sqlite_policy.checkpoint_mode,
        },
        "inputs": [{
            "code": item.code, "source_sha256": item.document.content_hash, "receipt_sha256": item.receipt_sha256,
            "source_receipt_sha256": item.document.receipt_hash, "received_at": item.document.received_at.isoformat(),
            "from_date": item.document.requested_from.isoformat(), "to_date": item.document.requested_to.isoformat(),
        } for item in inputs],
        "code_sha256": identities, "load_average_before": load_before, "load_average_after": os.getloadavg(),
        "candidate": request.candidate, "comparison": _comparison(rounds),
        "ingest_median_seconds": median(run["ingest_wall_seconds"] for run in rounds),
        "phase_medians_seconds": {phase: median(sum(
            _number(stage, "wall_seconds") for stage in run["phases"] if stage["phase"] == phase
        ) for run in rounds) for phase in phases},
        "phase_medians_by_candidate_seconds": {candidate: {phase: median(sum(
            _number(stage, "wall_seconds") for stage in run["phases"] if stage["phase"] == phase
        ) for run in rounds if run["candidate"] == candidate) for phase in phases}
        for candidate in dict.fromkeys(run["candidate"] for run in rounds)},
        "runs": rounds,
        "measurement": "DatabaseMeasurement without WAL sampler plus SqliteObservation; OS caches are not flushed; phase walls exclude observer setup",
    }
    report: dict[str, object] = {"status": "degraded" if source_unchanged and input_unchanged else "failed", "summary": summary}
    AtomicWriteJson(root / "baseline.json", report)
    return report


__all__ = [
    "IndustryCacheMeasurementRequest",
    "MeasureIndustryCache",
]
