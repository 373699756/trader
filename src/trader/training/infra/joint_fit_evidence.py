"""Strict, read-only benchmark evidence for enabling concurrent profile fitting."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from trader.infra.atomic_files.json import AtomicReadJson


@dataclass(frozen=True)
class JointFitEvidence:
    storage_kind: str
    cpu_count: int
    serial_input_hash: str
    parallel_input_hash: str
    serial_fit_seconds: float
    parallel_fit_seconds: float
    parallel_peak_rss_bytes: int
    status: str

    def supports_parallel(self, *, storage_kind: str, cpu_count: int) -> bool:
        return (
            self.status == "passed"
            and self.storage_kind == storage_kind == "ssd"
            and self.cpu_count == cpu_count
            and self.serial_input_hash == self.parallel_input_hash
            and self.parallel_fit_seconds <= 0.9 * self.serial_fit_seconds
            and self.parallel_peak_rss_bytes <= 5 * 1024**3
        )


def LoadJointFitEvidence(path: Path) -> JointFitEvidence | None:
    """Treat absent, malformed, or unverified measurements as a serial-only policy."""

    try:
        document = AtomicReadJson(path)
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "status",
        "storage_kind",
        "cpu_count",
        "serial_input_hash",
        "parallel_input_hash",
        "serial_fit_seconds",
        "parallel_fit_seconds",
        "parallel_peak_rss_bytes",
    }:
        return None
    if document["schema_version"] != "joint_fit_benchmark":
        return None
    hashes = (document["serial_input_hash"], document["parallel_input_hash"])
    if any(
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in hashes
    ):
        return None
    durations = (document["serial_fit_seconds"], document["parallel_fit_seconds"])
    if any(
        not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0
        for value in durations
    ):
        return None
    if (
        not isinstance(document["storage_kind"], str)
        or document["storage_kind"] not in {"hdd", "ssd", "unknown"}
        or type(document["cpu_count"]) is not int
        or document["cpu_count"] < 1
        or type(document["parallel_peak_rss_bytes"]) is not int
        or document["parallel_peak_rss_bytes"] < 1
        or not isinstance(document["status"], str)
        or document["status"] not in {"passed", "failed"}
    ):
        return None
    return JointFitEvidence(
        document["storage_kind"],
        document["cpu_count"],
        hashes[0],
        hashes[1],
        float(durations[0]),
        float(durations[1]),
        document["parallel_peak_rss_bytes"],
        document["status"],
    )


__all__ = ["JointFitEvidence", "LoadJointFitEvidence"]
