"""SQLite, file and bounded writer adapters for history synchronization."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, AbstractContextManager
from dataclasses import dataclass, field
from datetime import date
from functools import partial
from pathlib import Path
from uuid import uuid4

from trader.download.application.history_ports import (
    HistoryCurrentReviewPort, HistoryPriceBatchPort, HistoryReviewBusyError, HistoryReviewPreparation,
    HistorySyncAlreadyRunningError, HistorySyncFailureReport, HistorySyncIoError,
)
from trader.download.application.history_progress import PublishHistoryProgress
from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockSecurity
from trader.download.domain.history_control import HistoryDiskRequirement, HistorySnapshot, HistorySnapshotPartition
from trader.download.domain.history_download import HistoryBatchInput, HistoryPublication
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.history_reference import HistoryReferenceSnapshot
from trader.download.domain.history_sync import HistorySyncConfiguration, HistorySyncProgress, HistorySyncProgressPort, HistoryTwoStageSupplier
from trader.download.domain.price_review import PriceReviewBudget, PriceReviewBudgetExhausted
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.infra.history_control_index import (
    HistoryControlError, HistoryControlIndex, HistoryMaintenanceAlreadyRunningError,
    HistoryMaintenanceLock, InspectHistoryDisk,
)
from trader.download.infra.history_direct_prices import HistoryDirectPrices
from trader.download.infra.history_download_flow import (
    DownloadFailure, HistoryDownloadFlow, HistoryDownloadResources, HistoryInferredReviewResult, HistoryWriterDrainError,
)
from trader.infra.shutdown import ShutdownBudget
from trader.infra.future_cleanup import CloseAfterFutures
from trader.download.infra.history_download_index import HistoryDownloadIndex
from trader.download.infra.history_inferred_review import HistoryInferredReviewIndex, IsHistoryReviewBusy
from trader.download.infra.history_price_repair import HistoryPriceRepair
from trader.download.infra.history_prices import HistoryPricePartitionError
from trader.download.infra.history_reference_index import ReadBoundHistoryReference
from trader.download.infra.history_snapshot_publication import PublishHistorySnapshot
from trader.download.infra.history_sqlite_activity import HistorySqliteActivity
from trader.download.infra.history_sync_failure import HistoryFailureCode, HistoryFailureDetail, HistorySyncFailureError
from trader.download.infra.history_write_tuning import ChooseHistoryWriteTuning, HistoryWriteTuning
from trader.infra.disk_types import DiskKind
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.infra.sqlite_publication import PublishSqliteFile
from trader.infra.workers import WorkerExecutor

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class HistorySyncResources:
    """Technical resources selected by the composition root or isolated measurements."""

    worker_pool: WorkerExecutor | None = None
    writer_pool: WorkerExecutor | None = None
    write_tuning: HistoryWriteTuning | None = None
    disk_kind: DiskKind = "unknown"
    repair_only: bool = False
    price_review_budget: PriceReviewBudget | None = None
    batched_price_reads: bool = False
    exact_month_ranges: bool = False
    fingerprint_before_statistics: bool = False
    reuse_catalog_fingerprint: bool = False
    shutdown: ShutdownBudget = field(default_factory=lambda: ShutdownBudget(30.0))


@dataclass(frozen=True)
class _ReviewPriceFile:
    path: Path
    database: tuple[int, int, int] | None
    wal: tuple[int, int, int] | None


def _review_file_stamp(path: Path) -> tuple[int, int, int] | None:
    try:
        value = path.stat()
    except FileNotFoundError:
        return None
    return value.st_size, value.st_mtime_ns, value.st_ctime_ns


@dataclass(frozen=True)
class HistorySyncIo:
    configuration: HistorySyncConfiguration
    supplier: HistoryTwoStageSupplier
    cancel: Callable[[], bool]
    progress: HistorySyncProgressPort | None
    executor: WorkerExecutor
    writer: WorkerExecutor
    control: HistoryControlIndex
    index: HistoryDownloadIndex
    resources: HistorySyncResources
    sqlite_policy: SqliteIoPolicy = field(kw_only=True)

    @contextmanager
    def lease(self) -> Iterator[None]:
        try:
            with HistoryMaintenanceLock(self.configuration.history_root.with_name(".history.lock")) as lease:
                PublishHistoryProgress(self.progress, "disk_profile", "completed", current_item=self.resources.disk_kind)
                root = self.configuration.history_root
                if (root / "control.sqlite3").exists() or (
                    not (root / "catalog.sqlite3").exists()
                    and ((root / "partitions").exists() or any(root.glob("prices_*.sqlite3")))
                ):
                    raise RuntimeError("history_format_requires_rebuild")
                PublishHistoryProgress(self.progress, "initializing", "started", current_item="catalog_schema")
                self.control.initialize()
                PublishHistoryProgress(self.progress, "initializing", "completed", (1, 1), "catalog_schema")
                PublishHistoryProgress(self.progress, "initializing", "completed", (1, 1))
                try:
                    yield
                except HistoryWriterDrainError as exc:
                    lease.retain_until(exc.pending)
                    if self.cancel() or isinstance(exc.__cause__, KeyboardInterrupt):
                        raise KeyboardInterrupt from exc
                    raise
        except HistoryMaintenanceAlreadyRunningError as exc:
            raise HistorySyncAlreadyRunningError("history_maintenance_running") from exc
        except sqlite3.Error as exc:
            raise HistorySyncIoError(self.failure(exc)) from exc

    def has_space(self, minimum_free_bytes: int) -> bool:
        return InspectHistoryDisk(
            self.configuration.history_root, HistoryDiskRequirement(minimum_free_bytes, 0, 0, 0),
        ).sufficient

    def safe_snapshot(self) -> HistorySnapshot | None:
        try:
            state = self.control.load_published_state()
            return state.snapshot if state is not None else None
        except HistoryControlError:
            return None

    def finalize(self, result: HistoryMaintenanceStatus) -> None:
        if not result.published:
            return
        for path in sorted(self.configuration.history_root.glob("prices_[0-9][0-9][0-9][0-9].sqlite3")):
            if not Path(f"{path}.sha256").is_file():
                PublishSqliteFile(path, sqlite_policy=self.sqlite_policy)
        if not self.resources.reuse_catalog_fingerprint or result.state != "completed":
            self.control.publish_file()

    def failure(self, exc: BaseException) -> HistorySyncFailureReport:
        return HistorySyncFailureReport(
            HistoryFailureCode(exc), HistoryFailureDetail(exc),
            exc.stage if isinstance(exc, HistorySyncFailureError) else "synchronization",
            exc.item if isinstance(exc, HistorySyncFailureError) else None,
        )

    def report_failure(self, failure: HistorySyncFailureReport, progress: HistorySyncProgressPort | None) -> None:
        if progress is None:
            _LOGGER.error("history_sync_failure reason=%s diagnostic=%s", failure.reason, failure.detail)
            return
        try:
            progress.publish(HistorySyncProgress(
                failure.stage, "failed", 0, 1, failure.item,
                failure_reason=failure.reason, failure_detail=failure.detail,
            ))
        except OSError:
            pass

    def download_failure(self, exc: BaseException) -> str:
        return DownloadFailure(exc)

    def read_reference(self, source_contract: str) -> HistoryReferenceSnapshot:
        return ReadBoundHistoryReference(
            self.configuration.history_root, source_contract, sqlite_policy=self.sqlite_policy,
        )

    def publish(self, publication: HistoryPublication) -> None:
        PublishHistorySnapshot(self.control, publication)

    def new_repair_identity(self) -> str:
        return uuid4().hex

    def remaining_review_codes(self, budget: PriceReviewBudget) -> int:
        return HistoryInferredReviewIndex(
            self.configuration.history_root / "catalog.sqlite3", sqlite_policy=self.sqlite_policy,
        ).known_inferred_codes(budget)

    @contextmanager
    def open_batch(
        self, inputs: HistoryBatchInput, snapshot: HistorySnapshot | None,
    ) -> Iterator[HistoryPriceBatchPort]:
        root = self.configuration.history_root
        files = tuple(
            _ReviewPriceFile(path, _review_file_stamp(path), _review_file_stamp(Path(f"{path}-wal")))
            for path in sorted(root.glob("prices_[0-9][0-9][0-9][0-9].sqlite3"))
        )
        activity = HistorySqliteActivity(
            lambda: PublishHistoryProgress(self.progress, "persisting_partitions", "waiting"), self.cancel,
        )
        prices = HistoryDirectPrices(
            root, inputs.batch.sequence, snapshot, activity, fresh=inputs.fresh, sqlite_policy=self.sqlite_policy,
        )
        batch = _HistoryPriceBatch(
            self, inputs, prices, HistoryPriceRepair(root, sqlite_policy=self.sqlite_policy),
            self._flow(inputs, prices, snapshot), activity, files,
        )
        try:
            yield batch
        except HistoryWriterDrainError as exc:
            exc.pending = CloseAfterFutures(
                partial(self._seal_deferred_review, files, include_created=True), (exc.pending,),
            )
            raise
        except KeyboardInterrupt:
            self._seal_deferred_review(files, include_created=True)
            raise

    def current_review(self, inputs: HistoryBatchInput, snapshot: HistorySnapshot) -> HistoryCurrentReviewPort:
        prices = HistoryDirectPrices(
            self.configuration.history_root,
            inputs.batch.sequence,
            snapshot,
            HistorySqliteActivity(lambda: None, self.cancel),
            sqlite_policy=self.sqlite_policy,
        )
        return _CurrentHistoryReview(self, inputs, snapshot, prices, self._flow(inputs, prices, snapshot))

    def _seal_deferred_review(
        self, files: tuple[_ReviewPriceFile, ...], *, include_created: bool = False,
    ) -> None:
        """Keep old public readers trusted without publishing an auxiliary sequence.

        Writer resources have already closed. Mandatory fingerprint/fsync cleanup
        is measured wall time and may safely finish beyond the review deadline.
        """
        changed = tuple(
            item.path for item in files
            if item.database != _review_file_stamp(item.path)
            or item.wal != _review_file_stamp(Path(f"{item.path}-wal"))
        )
        if include_created:
            known = frozenset(item.path for item in files)
            changed += tuple(
                path for path in sorted(self.configuration.history_root.glob("prices_[0-9][0-9][0-9][0-9].sqlite3"))
                if path not in known
            )
        for completed, path in enumerate(changed, 1):
            PublishHistoryProgress(self.progress, "sealing_partitions", "started", (completed - 1, len(changed)), path.name)
            PublishSqliteFile(path, sqlite_policy=self.sqlite_policy)
            PublishHistoryProgress(self.progress, "sealing_partitions", "completed", (completed, len(changed)), path.name)
        self.control.publish_file()

    def _flow(
        self, inputs: HistoryBatchInput, prices: HistoryDirectPrices, current_snapshot: HistorySnapshot | None,
    ) -> HistoryDownloadFlow:
        return HistoryDownloadFlow(
            HistoryDownloadResources(
                self.configuration,
                inputs.context,
                self.supplier,
                prices,
                self.index,
                self.executor,
                self.writer,
                self.resources.write_tuning
                or ChooseHistoryWriteTuning(
                    self.resources.disk_kind,
                    self.configuration.write_batches,
                    fresh=current_snapshot is None,
                ),
                self.cancel,
                self.progress,
                self.resources.shutdown,
            ),
            sqlite_policy=self.sqlite_policy,
            batched_price_reads=self.resources.batched_price_reads,
        )

    def _review_batch(
        self,
        inputs: HistoryBatchInput,
        prices: HistoryDirectPrices,
        flow: HistoryDownloadFlow,
        files: tuple[_ReviewPriceFile, ...],
        budget: PriceReviewBudget,
    ) -> None:
        try:
            review = HistoryInferredReviewIndex(
                self.configuration.history_root / "catalog.sqlite3", sqlite_policy=self.sqlite_policy,
            )
            planned = review.plan(inputs.context, prices, inputs.batch.sequence, self.configuration.price_review, budget, self.cancel)
            results = flow.prepare_inferred_reviews(planned, budget, inputs.batch.sequence)
            flow.commit_inferred_reviews(results, inputs.batch.sequence, budget)
        except PriceReviewBudgetExhausted:
            pass
        except KeyboardInterrupt:
            self._seal_deferred_review(files, include_created=True)
            raise
        except sqlite3.OperationalError as exc:
            if not IsHistoryReviewBusy(exc):
                self._seal_deferred_review(files, include_created=True)
                raise
            _LOGGER.warning("history_inferred_review_deferred reason=sqlite_busy")
        except HistoryPricePartitionError as exc:
            if not isinstance(exc.__cause__, sqlite3.OperationalError) or not IsHistoryReviewBusy(exc.__cause__):
                self._seal_deferred_review(files, include_created=True)
                raise
            _LOGGER.warning("history_inferred_review_deferred reason=sqlite_busy")
        except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
            self._seal_deferred_review(files, include_created=True)
            raise
        finally:
            budget.pause()


@dataclass
class _CurrentHistoryReview:
    owner: HistorySyncIo
    inputs: HistoryBatchInput
    snapshot: HistorySnapshot
    prices: HistoryDirectPrices
    flow: HistoryDownloadFlow
    results: tuple[HistoryInferredReviewResult, ...] | None = None
    files: tuple[_ReviewPriceFile, ...] = ()

    @contextmanager
    def activity(self) -> Iterator[None]:
        try:
            yield
        except sqlite3.OperationalError as exc:
            if IsHistoryReviewBusy(exc):
                raise HistoryReviewBusyError(self.owner.failure(exc)) from exc
            raise HistorySyncIoError(self.owner.failure(exc)) from exc
        except sqlite3.Error as exc:
            raise HistorySyncIoError(self.owner.failure(exc)) from exc
        except HistoryPricePartitionError as exc:
            if isinstance(exc.__cause__, sqlite3.OperationalError) and IsHistoryReviewBusy(exc.__cause__):
                raise HistoryReviewBusyError(self.owner.failure(exc), partition=True) from exc
            raise

    def prepare(self, budget: PriceReviewBudget) -> HistoryReviewPreparation:
        owner, inputs = self.owner, self.inputs
        review = HistoryInferredReviewIndex(
            owner.configuration.history_root / "catalog.sqlite3", sqlite_policy=owner.sqlite_policy,
        )
        planned = review.plan(
            inputs.context, self.prices, self.snapshot.sequence, owner.configuration.price_review, budget, owner.cancel,
        )
        self.results = self.flow.prepare_inferred_reviews(planned, budget, inputs.batch.sequence)
        revised_dates = tuple(sorted({
            revision.trade_date for result in self.results if result.price is not None
            for revision in result.price.revisions
        }))
        self.files = tuple(
            _ReviewPriceFile(path, _review_file_stamp(path), _review_file_stamp(Path(f"{path}-wal")))
            for path in (
                owner.configuration.history_root / f"prices_{year:04d}.sqlite3"
                for year in sorted({day.year for day in revised_dates})
            )
        )
        return HistoryReviewPreparation(revised_dates)

    def commit(self, sequence: int, budget: PriceReviewBudget) -> None:
        if self.results is None:
            raise RuntimeError("history_review_not_prepared")
        self.flow.commit_inferred_reviews(self.results, sequence, budget)

    def seal_deferred(self) -> None:
        self.owner._seal_deferred_review(self.files)


@dataclass(frozen=True)
class _HistoryPriceBatch:
    owner: HistorySyncIo
    inputs: HistoryBatchInput
    prices: HistoryDirectPrices
    repair: HistoryPriceRepair
    flow: HistoryDownloadFlow
    activity: HistorySqliteActivity
    files: tuple[_ReviewPriceFile, ...]

    def replay_repairs(self, codes: frozenset[str]) -> None:
        self.repair.replay(self.inputs.batch.sequence, codes=codes)

    def download_tencent(self) -> None:
        self.flow.tencent()

    def prepare_retry_pass(self) -> None:
        self.prices.prepare_retry_pass()

    def reserve_repair(self, code: str, dates: tuple[date, ...], batch: str, *, explicit: bool) -> bool:
        return bool(self.repair.reserve(code, dates, batch, explicit=explicit))

    def repair_source(self, code: str, sequence: int) -> str:
        return self.repair.source(code, sequence)

    def read_prices(self, code: str, dates: tuple[date, ...]) -> tuple[BaoStockDailyCell, ...]:
        return self.prices.read(code, dates)

    def replace_repair(
        self, security: BaoStockSecurity, dates: tuple[date, ...], incoming: PublishedHistoryWindow, sequence: int,
    ) -> None:
        self.repair.replace(security, dates, incoming, self.prices, sequence)

    def fail_repair(self, code: str, batch: str, reason: str) -> None:
        self.repair.fail(code, batch, reason)

    def publication_activity(self) -> AbstractContextManager[None]:
        return self.activity.reporting(lambda: PublishHistoryProgress(
            self.owner.progress, "preparing_publication", "waiting",
        ))

    def remaining_gaps(self) -> Mapping[str, tuple[date, ...]]:
        return self.owner.index.gaps_by_code(
            self.inputs.calendar.open_dates, retryable_only=False, activity=self.activity,
        )

    def record_missing(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> None:
        self.activity.tick()
        self.prices.record_missing(security, dates)

    def review_inferred(self, budget: PriceReviewBudget) -> None:
        self.owner._review_batch(self.inputs, self.prices, self.flow, self.files, budget)

    def publishable_partitions(self, dates: tuple[date, ...]) -> tuple[HistorySnapshotPartition, ...]:
        return self.prices.publishable_partitions(
            dates,
            lambda stage, state, done, total, item: PublishHistoryProgress(
                self.owner.progress, stage, state, (done, total), item,
            ),
            exact_month_ranges=self.owner.resources.exact_month_ranges,
            fingerprint_before_statistics=self.owner.resources.fingerprint_before_statistics,
        )


__all__ = ["HistorySyncIo", "HistorySyncResources"]
