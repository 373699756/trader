"""Immutable context and evidence for a failed download operation."""

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class DownloadFailureConfiguration:
    retention_limit: int
    query_limit: int


@dataclass(frozen=True, slots=True)
class DownloadFailureContext:
    stage: str
    code: str = ""
    supplier: str = "local"
    attempt: int = 1
    maximum_attempts: int = 1
    elapsed_seconds: float | None = None
    timeout_seconds: float | None = None
    detail: str = ""
    window_start: date | None = None
    window_end: date | None = None
    endpoint: str = ""


@dataclass(frozen=True, slots=True)
class DownloadFailureRecord:
    run_id: str
    occurred_at: datetime
    context: DownloadFailureContext
    reason: str
    causes: str
    diagnostic: str
