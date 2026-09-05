"""Application contracts for the explicit BaoStock process runner."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

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
BaoStockRuntimePhase = Literal[
    "preflight",
    "checkpoint_loading",
    "supplier_login",
    "trading_calendar",
    "security_universe",
    "database_initializing",
    "worker_starting",
    "downloading",
    "merging",
]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class BaoStockRuntimeRequest:
    runtime_dir: Path
    sessions: int = 2000
    # BaoStock's anonymous service blacklists bursty/concurrent history calls.
    # Keep the old single-process download shape as the safe default.
    workers: int = 1
    timeout_seconds: float = 60.0
    retries: int = 2

    def validate(self, repository_root: Path) -> None:
        if not self.runtime_dir.is_absolute():
            raise ValueError("BaoStock runtime directory must be absolute")
        # The explicit history command persists in the project-root data/history
        # directory by default; callers may provide any absolute offline directory.
        if isinstance(self.sessions, bool) or not 1 <= self.sessions <= 2000:
            raise ValueError("BaoStock sessions must be in 1..2000")
        if isinstance(self.workers, bool) or self.workers != 1:
            raise ValueError("BaoStock history download must use exactly one worker")
        if (
            isinstance(self.timeout_seconds, bool)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 60
        ):
            raise ValueError("BaoStock timeout must be in (0, 60]")
        if isinstance(self.retries, bool) or not 0 <= self.retries <= 2:
            raise ValueError("BaoStock retries must be in 0..2")


@dataclass(frozen=True)
class BaoStockRuntimeProgress:
    phase: BaoStockRuntimePhase
    source: str = "baostock"
    current_code: str = ""
    sessions: int = 2000
    universe_count: int = 0
    completed_codes: int = 0
    failed_codes: int = 0
    expected_records: int = 0
    downloaded_records: int = 0
    active_workers: int = 0
    rate_limit_cooldown_seconds: float = 0.0
    last_failure_reason: str = ""
    schema_version: str = "baostock_runtime_progress"

    def __post_init__(self) -> None:
        _validate_progress_source(self.source, self.current_code, self.rate_limit_cooldown_seconds)
        if not 1 <= self.sessions <= 2000:
            raise ValueError("BaoStock progress sessions must be in 1..2000")
        code_counts = (self.universe_count, self.completed_codes, self.failed_codes, self.active_workers)
        if any(isinstance(value, bool) or value < 0 for value in code_counts):
            raise ValueError("BaoStock progress code counts must be non-negative integers")
        if self.completed_codes + self.failed_codes > self.universe_count:
            raise ValueError("BaoStock progress code counts exceed the universe")
        record_counts = (self.expected_records, self.downloaded_records)
        if any(isinstance(value, bool) or value < 0 for value in record_counts):
            raise ValueError("BaoStock progress record counts must be non-negative integers")
        if self.downloaded_records > self.expected_records:
            raise ValueError("BaoStock progress record counts exceed the expected total")
        if self.active_workers > 2:
            raise ValueError("BaoStock progress active workers exceed the process cap")
        if self.last_failure_reason and (
            len(self.last_failure_reason) > 64
            or not self.last_failure_reason.isascii()
            or not all(character.isalnum() or character == "_" for character in self.last_failure_reason)
        ):
            raise ValueError("BaoStock progress failure reason is invalid")

    @property
    def checkpointed_codes(self) -> int:
        return self.completed_codes + self.failed_codes

    @property
    def remaining_codes(self) -> int:
        """Return codes that still need a successful durable download."""
        return self.universe_count - self.completed_codes


def _validate_progress_source(source: str, current_code: str, cooldown_seconds: float) -> None:
    if source != "baostock":
        raise ValueError("BaoStock progress source must be baostock")
    if current_code and (len(current_code) != 6 or not current_code.isdigit()):
        raise ValueError("BaoStock progress current code is invalid")
    if not math.isfinite(cooldown_seconds) or cooldown_seconds < 0:
        raise ValueError("BaoStock progress cooldown must be finite and non-negative")


class BaoStockRuntimeProgressPort(Protocol):
    def publish(self, progress: BaoStockRuntimeProgress) -> None: ...


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
    schema_version: str = "baostock_runtime_status"

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


__all__ = [
    "BaoStockRuntimePhase",
    "BaoStockRuntimeProgress",
    "BaoStockRuntimeProgressPort",
    "BaoStockRuntimeRequest",
    "BaoStockRuntimeState",
    "BaoStockRuntimeStatus",
]
