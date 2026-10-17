from __future__ import annotations

from pathlib import Path

from trader.infra.atomic_files.json import AtomicWriteJson
from trader.training.infra.joint_fit_evidence import LoadJointFitEvidence


def test_parallel_fit_evidence_requires_same_input_and_measured_speedup(tmp_path: Path) -> None:
    path = tmp_path / "benchmark.json"
    payload = {
        "schema_version": "joint_fit_benchmark",
        "status": "passed",
        "storage_kind": "ssd",
        "cpu_count": 8,
        "serial_input_hash": "a" * 64,
        "parallel_input_hash": "a" * 64,
        "serial_fit_seconds": 100.0,
        "parallel_fit_seconds": 85.0,
        "parallel_peak_rss_bytes": 4 * 1024**3,
    }
    AtomicWriteJson(path, payload)
    evidence = LoadJointFitEvidence(path)
    assert evidence is not None
    assert evidence.supports_parallel(storage_kind="ssd", cpu_count=8)

    AtomicWriteJson(path, {**payload, "parallel_input_hash": "b" * 64})
    evidence = LoadJointFitEvidence(path)
    assert evidence is not None
    assert not evidence.supports_parallel(storage_kind="ssd", cpu_count=8)

    AtomicWriteJson(path, {**payload, "parallel_fit_seconds": 99.0})
    evidence = LoadJointFitEvidence(path)
    assert evidence is not None
    assert not evidence.supports_parallel(storage_kind="ssd", cpu_count=8)


def test_invalid_or_missing_parallel_evidence_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "benchmark.json"
    assert LoadJointFitEvidence(path) is None
    AtomicWriteJson(path, {"schema_version": "joint_fit_benchmark", "status": "passed"})
    assert LoadJointFitEvidence(path) is None
    AtomicWriteJson(
        path,
        {
            "schema_version": "joint_fit_benchmark",
            "status": [],
            "storage_kind": [],
            "cpu_count": 4,
            "serial_input_hash": "a" * 64,
            "parallel_input_hash": "a" * 64,
            "serial_fit_seconds": 100.0,
            "parallel_fit_seconds": 80.0,
            "parallel_peak_rss_bytes": 4 * 1024**3,
        },
    )
    assert LoadJointFitEvidence(path) is None
