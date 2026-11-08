"""Immutable download facts delivered to an explicitly injected progress port."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from trader.download.application.progress_message import ProgressLevel, ProgressMessage, ProgressState
from trader.download.application.stock_download_progress import StockDownloadProgress
from trader.download.domain.qfq_window import QfqPendingReason


@dataclass(frozen=True)
class ProgressCount:
    label: str
    value: int
    unit: str = ""


@dataclass(frozen=True)
class ProgressRatio:
    label: str
    completed: int
    total: int


@dataclass(frozen=True)
class ProgressMeasure:
    label: str
    value: float
    unit: str


@dataclass(frozen=True)
class ProgressDate:
    label: str
    value: date


@dataclass(frozen=True)
class ProgressSource:
    value: str


ProgressDetail = str | ProgressCount | ProgressRatio | ProgressMeasure | ProgressDate | ProgressSource


@dataclass(frozen=True)
class QfqNotice:
    stage: str
    details: tuple[ProgressDetail, ...]
    level: ProgressLevel = ProgressLevel.NORMAL
    state: ProgressState = "处理中"
    finished: bool = False
    label: str | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.details, tuple):
            raise TypeError("download_progress_details_require_typed_tuple")


@dataclass(frozen=True)
class QfqSummary:
    stage: str
    total: int
    processed: int
    completed: int
    pending: int
    skipped: int
    not_applicable: int
    no_trade_data: int
    latest_suspended: int
    latest_inferred_suspended: int
    latest_missing: int
    unresolved_supplement_codes: int
    changed_files: int
    rows: int
    write_seconds: float
    level: ProgressLevel


@dataclass(frozen=True)
class QfqPendingSummary:
    reasons: tuple[QfqPendingReason, ...]
    level: ProgressLevel


DownloadProgressEvent = ProgressMessage | StockDownloadProgress | QfqNotice | QfqSummary | QfqPendingSummary


class DownloadProgressPort(Protocol):
    def __call__(self, event: DownloadProgressEvent, /) -> None: ...


__all__ = [
    "DownloadProgressEvent", "DownloadProgressPort", "ProgressCount", "ProgressRatio",
    "ProgressMeasure", "ProgressDate", "ProgressSource", "ProgressDetail",
    "QfqNotice", "QfqSummary", "QfqPendingSummary",
]
