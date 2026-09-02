"""Application contracts for the explicit BaoStock process runner."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

BaoStockRuntimeState = Literal[
    "not_started",
    "completed",
    "completed_with_failures",
    "cancelled",
    "locked",
    "dependency_unavailable",
    "failed",
    "resource_blocked",
]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class BaoStockRuntimeRequest:
    runtime_dir: Path
    sessions: int = 2000
    workers: int = 2
    timeout_seconds: float = 60.0
    retries: int = 2

    def validate(self, repository_root: Path) -> None:
        if not self.runtime_dir.is_absolute():
            raise ValueError("BaoStock runtime directory must be absolute")
        try:
            self.runtime_dir.resolve().relative_to(repository_root.resolve())
        except ValueError:
            pass
        else:
            raise ValueError("BaoStock runtime directory must be outside repository")
        if isinstance(self.sessions, bool) or not 1 <= self.sessions <= 2000:
            raise ValueError("BaoStock sessions must be in 1..2000")
        if isinstance(self.workers, bool) or self.workers not in (1, 2):
            raise ValueError("BaoStock workers must be 1 or 2")
        if (
            isinstance(self.timeout_seconds, bool)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 60
        ):
            raise ValueError("BaoStock timeout must be in (0, 60]")
        if isinstance(self.retries, bool) or not 0 <= self.retries <= 2:
            raise ValueError("BaoStock retries must be in 0..2")


@dataclass(frozen=True)
class BaoStockRuntimeStatus:
    state: BaoStockRuntimeState = "not_started"
    sessions: int = 2000
    shard_count: int = 0
    universe_count: int = 0
    completed_codes: int = 0
    failed_codes: int = 0
    peak_rss_mb: float = 0.0
    manifest_hash: str = ""
    coverage_status: str = "historical_data_insufficient"
    historical_effective_facts_status: str = "historical_data_insufficient"
    historical_effective_facts_hash: str = ""
    v3_dataset_status: str = "historical_data_insufficient"
    v3_dataset_hash: str = ""
    failure_reasons: tuple[str, ...] = ()
    production_authority: bool = False
    point_in_time_parity: bool = False
    schema_version: str = "baostock_runtime_status_v1"

    def __post_init__(self) -> None:
        if self.production_authority or self.point_in_time_parity:
            raise ValueError("BaoStock runtime cannot authorize production or point-in-time parity")
        if not 1 <= self.sessions <= 2000:
            raise ValueError("BaoStock runtime sessions must be in 1..2000")
        hashes = (self.manifest_hash, self.historical_effective_facts_hash, self.v3_dataset_hash)
        if any(value and _SHA256.fullmatch(value) is None for value in hashes):
            raise ValueError("BaoStock runtime artifact hash must be SHA-256")
        counts = (self.shard_count, self.universe_count, self.completed_codes, self.failed_codes)
        if any(isinstance(value, bool) or value < 0 for value in counts):
            raise ValueError("BaoStock runtime counts must be non-negative integers")
        if self.completed_codes + self.failed_codes > self.universe_count:
            raise ValueError("BaoStock runtime code counts exceed the universe")
        if not math.isfinite(self.peak_rss_mb) or self.peak_rss_mb < 0:
            raise ValueError("BaoStock runtime RSS must be finite and non-negative")
        object.__setattr__(self, "failure_reasons", tuple(sorted(set(self.failure_reasons))))


__all__ = ["BaoStockRuntimeRequest", "BaoStockRuntimeState", "BaoStockRuntimeStatus"]
