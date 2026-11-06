"""Typed dependencies of the history synchronization use case."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockSecurity
from trader.download.domain.history_control import (
    HistoryCalendarIdentity, HistoryControlState, HistoryPublishedControlState,
    HistorySnapshot, HistorySnapshotPartition, HistorySourceIdentity, HistoryUniverseIdentity,
)
from trader.download.domain.history_download import (
    HistoryBatchInput, HistoryDownloadBatch, HistoryDownloadTask, HistoryPublication,
)
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus, HistoryPriceGapSummary
from trader.download.domain.history_reference import HistoryReferenceSnapshot
from trader.download.domain.history_sync import HistorySyncProgressPort, HistorySyncProgressStage
from trader.download.domain.price_review import PriceReviewBudget
from trader.download.domain.published_history import PublishedHistoryWindow


@dataclass(frozen=True)
class HistorySyncFailureReport:
    reason: str
    detail: str
    stage: HistorySyncProgressStage = "synchronization"
    item: str | None = None


class HistorySyncAlreadyRunningError(RuntimeError):
    """The infrastructure could not acquire this history operation's lease."""


class HistoryReviewDeferredError(RuntimeError):
    """A begun auxiliary review needs a later recovery attempt."""


class HistorySyncIoError(RuntimeError):
    """A physical operation failed with its bounded, explicit failure projection."""

    def __init__(self, failure: HistorySyncFailureReport) -> None:
        super().__init__(failure.reason)
        self.failure = failure


class HistoryReviewBusyError(HistorySyncIoError):
    """SQLite review contention, retaining whether a partition operation failed."""

    def __init__(self, failure: HistorySyncFailureReport, *, partition: bool = False) -> None:
        super().__init__(failure)
        self.partition = partition


@dataclass(frozen=True)
class HistoryReviewPreparation:
    revised_dates: tuple[date, ...]


class HistoryCurrentReviewPort(Protocol):
    def activity(self) -> AbstractContextManager[None]: ...
    def prepare(self, budget: PriceReviewBudget) -> HistoryReviewPreparation: ...
    def commit(self, sequence: int, budget: PriceReviewBudget) -> None: ...
    def seal_deferred(self) -> None: ...


class HistoryControlPort(Protocol):
    def load_published_state(self) -> HistoryPublishedControlState | None: ...
    def load_state(self) -> HistoryControlState: ...
    def save_inputs(
        self,
        parents: tuple[HistorySourceIdentity, HistoryCalendarIdentity, HistoryUniverseIdentity],
        reference: HistoryReferenceSnapshot,
        publication_id: str,
    ) -> None: ...
    def save_reference(self, reference: HistoryReferenceSnapshot, publication_id: str) -> None: ...


class HistoryTaskPort(Protocol):
    def pending(self, published_sequence: int) -> HistoryDownloadBatch | None: ...
    def begin(self, batch: HistoryDownloadBatch, tasks: tuple[HistoryDownloadTask, ...]) -> None: ...
    def tasks(self, *, codes: frozenset[str] | None = None) -> tuple[HistoryDownloadTask, ...]: ...
    def gaps_by_code(
        self, calendar_dates: tuple[date, ...] | None = None, *, retryable_only: bool = True,
    ) -> Mapping[str, tuple[date, ...]]: ...
    def gap_summary(self, codes: tuple[str, ...], *, calendar_dates: tuple[date, ...]) -> HistoryPriceGapSummary: ...
    def rebind_context(
        self,
        previous: HistoryDownloadBatch,
        current: HistoryDownloadBatch,
        *,
        tasks: tuple[HistoryDownloadTask, ...] | None = None,
        reopened_codes: frozenset[str] = frozenset(),
    ) -> None: ...


class HistoryMaintenancePort(Protocol):
    def lease(self) -> AbstractContextManager[None]: ...
    def has_space(self, minimum_free_bytes: int) -> bool: ...
    def safe_snapshot(self) -> HistorySnapshot | None: ...
    def finalize(self, result: HistoryMaintenanceStatus) -> None: ...
    def failure(self, exc: BaseException) -> HistorySyncFailureReport: ...
    def report_failure(self, failure: HistorySyncFailureReport, progress: HistorySyncProgressPort | None) -> None: ...
    def download_failure(self, exc: BaseException) -> str: ...
    def read_reference(self, source_contract: str) -> HistoryReferenceSnapshot: ...
    def publish(self, publication: HistoryPublication) -> None: ...
    def new_repair_identity(self) -> str: ...


class HistoryPriceBatchPort(Protocol):
    def replay_repairs(self, codes: frozenset[str]) -> None: ...
    def download_tencent(self) -> None: ...
    def prepare_retry_pass(self) -> None: ...
    def reserve_repair(self, code: str, dates: tuple[date, ...], batch: str, *, explicit: bool) -> bool: ...
    def repair_source(self, code: str, sequence: int) -> str: ...
    def read_prices(self, code: str, dates: tuple[date, ...]) -> tuple[BaoStockDailyCell, ...]: ...
    def replace_repair(
        self, security: BaoStockSecurity, dates: tuple[date, ...], incoming: PublishedHistoryWindow, sequence: int,
    ) -> None: ...
    def fail_repair(self, code: str, batch: str, reason: str) -> None: ...
    def publication_activity(self) -> AbstractContextManager[None]: ...
    def remaining_gaps(self) -> Mapping[str, tuple[date, ...]]: ...
    def record_missing(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> None: ...
    def review_inferred(self, budget: PriceReviewBudget) -> None: ...
    def publishable_partitions(self, dates: tuple[date, ...]) -> tuple[HistorySnapshotPartition, ...]: ...


class HistoryPriceWorkPort(Protocol):
    def open_batch(
        self, inputs: HistoryBatchInput, snapshot: HistorySnapshot | None,
    ) -> AbstractContextManager[HistoryPriceBatchPort]: ...
    def current_review(self, inputs: HistoryBatchInput, snapshot: HistorySnapshot) -> HistoryCurrentReviewPort: ...
    def remaining_review_codes(self, budget: PriceReviewBudget) -> int: ...


@dataclass(frozen=True)
class HistorySyncDependencies:
    maintenance: HistoryMaintenancePort
    control: HistoryControlPort
    tasks: HistoryTaskPort
    prices: HistoryPriceWorkPort


__all__ = [
    "HistoryControlPort",
    "HistoryCurrentReviewPort",
    "HistoryMaintenancePort",
    "HistoryPriceBatchPort",
    "HistoryPriceWorkPort",
    "HistoryReviewDeferredError",
    "HistoryReviewBusyError",
    "HistoryReviewPreparation",
    "HistorySyncAlreadyRunningError",
    "HistorySyncDependencies",
    "HistorySyncFailureReport",
    "HistorySyncIoError",
    "HistoryTaskPort",
]
