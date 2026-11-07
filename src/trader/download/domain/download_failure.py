"""Immutable context and evidence for a failed download operation."""

from dataclasses import dataclass
from datetime import date, datetime
import math
import re
from typing import Literal


class DownloadValidationFailure(ValueError):
    """Explicit scalar differences; callers never attach supplier bodies or local paths."""

    def __init__(self, reason: str, field: str, expected: str | int, actual: str | int) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,79}", reason) or not re.fullmatch(r"[A-Za-z0-9_.]{1,80}", field):
            raise ValueError("download_validation_identity_invalid")
        if any(
            type(value) not in {str, int}
            or isinstance(value, str) and not re.fullmatch(r"[A-Za-z0-9_.:+ =,<>-]{1,120}", value)
            for value in (expected, actual)
        ):
            raise ValueError("download_validation_difference_invalid")
        super().__init__(reason)
        self.field, self.expected, self.actual = field, expected, actual


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
    operation_id: int | None = None
    operation_kind: str = ""
    component: str = ""
    object_id: str = ""
    revision: str = ""
    name: str = ""
    title: str = ""
    market: str = ""
    page: int | None = None
    page_size: int | None = None
    queued_seconds: float | None = None
    raw_saved: bool | None = None
    text_extracted: bool | None = None
    collection_committed: bool | None = None
    publication_committed: bool | None = None

    def __post_init__(self) -> None:
        if any(value is not None and (type(value) is not int or value < 1)
               for value in (self.operation_id, self.page, self.page_size)):
            raise ValueError("download_failure_operation_invalid")
        if self.queued_seconds is not None and (not math.isfinite(self.queued_seconds) or self.queued_seconds < 0):
            raise ValueError("download_failure_queue_time_invalid")
        if any(value is not None and type(value) is not bool for value in (
            self.raw_saved, self.text_extracted, self.collection_committed, self.publication_committed,
        )):
            raise ValueError("download_failure_boundary_invalid")


@dataclass(frozen=True, slots=True)
class DownloadFailureRecord:
    run_id: str
    occurred_at: datetime
    context: DownloadFailureContext
    reason: str
    causes: str
    diagnostic: str


@dataclass(frozen=True, slots=True)
class DownloadFailureObservation:
    record: DownloadFailureRecord
    persistence: Literal["pending", "saved", "failed"] = "pending"
    recording_diagnostic: str = ""

    def __post_init__(self) -> None:
        if self.persistence not in {"pending", "saved", "failed"}:
            raise ValueError("download_failure_persistence_invalid")
