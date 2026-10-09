"""Immutable supplier health values shared without concrete vendor dependencies."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ModelIndustrySourceHealth:
    planned_count: int = 0
    success_count: int = 0
    error_count: int = 0
    timeout_count: int = 0
    snapshot_rows: int = 0
    invalid_rows: int = 0
    last_latency_ms: float = 0.0
    last_error: str | None = None
    last_source_time: datetime | None = None
    timeout_seconds: float = 0.0


@dataclass(frozen=True)
class SecurityMasterSourceHealth:
    enabled: bool
    planned_count: int
    success_count: int
    error_count: int
    timeout_count: int
    consecutive_failures: int
    last_latency_ms: float | None
    p50_latency_ms: float | None
    p95_latency_ms: float | None
    last_error: str | None
    snapshot_rows: int
    listing_date_rows: int
    last_source_time: datetime | None
    timeout_seconds: float


@dataclass(frozen=True)
class ReferenceSourceHealth:
    enabled: bool
    access_points: int
    history_mode: str
    minute_call_limit: int
    daily_call_limit: int | None
    process_api_attempts_last_minute: int
    process_api_attempts_today: int
    process_remaining_calls_today: int | None
    local_rate_limit_count: int
    planned_count: int
    success_count: int
    error_count: int
    consecutive_failures: int
    circuit_open: bool
    timeout_count: int
    last_latency_ms: float
    p50_latency_ms: float | None
    p95_latency_ms: float | None
    degraded_reason: str | None
    timeout_seconds: float
    data_age_seconds: float | None
