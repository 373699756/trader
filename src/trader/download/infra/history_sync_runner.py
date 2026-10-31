"""Direct monthly history downloads with durable metadata and logical snapshot publication."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy
from dataclasses import field

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from pathlib import Path
from uuid import uuid4
from typing import Literal, TypeAlias
from zoneinfo import ZoneInfo

from trader.download.domain.history_control import (
    HistoryCalendarIdentity,
    HistoryDiskRequirement,
    HistorySecurityIdentity,
    HistorySnapshot,
    HistorySourceIdentity,
    HistorySyncCheckpoint,
    HistoryUniverseIdentity,
)
from trader.download.domain.history_maintenance import (
    HistoryMaintenanceState,
    HistoryMaintenanceStatus,
    HistoryPriceGapSummary,
)
from trader.download.domain.history_price_qualification import HISTORY_UNIVERSE_CONTRACT
from trader.download.domain.history_reference import HistoryReferenceSnapshot
from trader.download.domain.history_sync import (
    HistorySupplierContext,
    HistoryRepairSummary,
    HistorySyncConfiguration,
    HistorySyncProgress,
    HistorySyncProgressPort,
    HistorySyncProgressStage,
    HistoryTwoStageSupplier,
)
from trader.download.domain.qfq_window import CompletedDailyCutoff, MergeDailyTail
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryWindow
from trader.download.domain.security_eligibility import IsStSecurityName
from trader.download.infra.history_control_index import (
    HistoryControlError,
    HistoryControlIndex,
    HistoryMaintenanceAlreadyRunningError,
    HistoryMaintenanceLock,
    InspectHistoryDisk,
)
from trader.download.infra.history_direct_prices import HistoryDirectPrices
from trader.download.infra.history_price_repair import HistoryPriceRepair
from trader.download.infra.history_download_flow import DownloadFailure
from trader.download.infra.history_download_flow import HistoryDownloadFlow, HistoryDownloadResources
from trader.download.infra.history_download_index import HistoryDownloadBatch, HistoryDownloadIndex, HistoryDownloadTask
from trader.download.infra.history_reference_index import (
    ReadBoundHistoryReference,
)
from trader.download.infra.history_snapshot_publication import HistoryPublication, PublishHistorySnapshot
from trader.download.infra.history_sqlite_activity import HistorySqliteActivity
from trader.download.infra.history_sync_failure import (
    HistoryFailureCode,
    HistoryFailureDetail,
    HistorySyncFailureError,
)
from trader.download.infra.history_write_tuning import (
    ChooseHistoryWriteTuning,
    HistoryWriteTuning,
)
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.storage_types import StorageKind
from trader.infra.workers import InjectedExecutor, WorkerExecutor

Clock: TypeAlias = Callable[[], datetime]
Cancellation: TypeAlias = Callable[[], bool]
_SHANGHAI = ZoneInfo("Asia/Shanghai")
# Keep the persisted source identity timestamp stable; target-day selection uses CompletedDailyCutoff.
_BAOSTOCK_DAILY_READY = time(15, 10)
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _BatchInput:
    context: HistorySupplierContext
    source: HistorySourceIdentity
    calendar: HistoryCalendarIdentity
    universe: HistoryUniverseIdentity
    reference: HistoryReferenceSnapshot
    batch: HistoryDownloadBatch
    fresh: bool


@dataclass(frozen=True)
class _HistorySync:
    configuration: HistorySyncConfiguration
    supplier: HistoryTwoStageSupplier
    observed_at: datetime
    cancel: Cancellation
    progress: HistorySyncProgressPort | None
    executor: WorkerExecutor
    writer: WorkerExecutor
    control: HistoryControlIndex
    index: HistoryDownloadIndex
    storage_kind: StorageKind
    write_tuning: HistoryWriteTuning | None
    repair_only: bool = False

    sqlite_policy: SqliteIoPolicy = field(kw_only=True)

    def execute(self) -> HistoryMaintenanceStatus:
        published = False
        while True:
            state = self.control.load_published_state()
            current_snapshot = state.snapshot if state is not None else None
            previous = frozenset(item.code for item in state.universe.securities) if state is not None else frozenset()
            if not _has_sufficient_disk(self.configuration.history_root, self.configuration.minimum_free_bytes):
                return _status("blocked", "disk_space_insufficient", self.configuration, current_snapshot)
            pending = self.index.pending(current_snapshot.sequence if current_snapshot else 0)
            if self.repair_only and pending is not None and any(not task.tencent_done and task.dates for task in self.index.tasks()):
                return _status("blocked", "history_download_incomplete", self.configuration, current_snapshot)
            target = pending.cutoff if pending else current_snapshot.data_cutoff if self.repair_only and current_snapshot is not None else CompletedDailyCutoff(self.observed_at)
            inputs = self._prepare(current_snapshot, pending, target, previous)
            if inputs is None:
                unresolved = self.index.gap_summary(tuple(previous)) if current_snapshot is not None else None
                return _status(
                    "completed" if published else "already_current",
                    "history_price_gaps_remaining" if unresolved and unresolved.cells else None,
                    self.configuration,
                    current_snapshot,
                    unresolved_price_cells=unresolved.cells if unresolved else 0,
                    price_gap_summary=unresolved,
                )
            snapshot = self._download(inputs, current_snapshot)
            published = True
            if not self.repair_only and pending is not None and target < CompletedDailyCutoff(self.observed_at):
                continue
            unresolved = self.index.gap_summary(tuple(item.code for item in inputs.context.universe))
            return _status(
                "completed",
                "history_price_gaps_remaining" if unresolved and unresolved.cells else None,
                self.configuration,
                snapshot,
                unresolved_price_cells=unresolved.cells if unresolved else 0,
                price_gap_summary=unresolved,
            )

    def _prepare(
        self,
        current_snapshot: HistorySnapshot | None,
        pending: HistoryDownloadBatch | None,
        target: date,
        previous: frozenset[str],
    ) -> _BatchInput | None:
        _publish_progress(self.progress, "loading_context", "started")
        context = self.supplier.load_context(target, self.configuration.sessions)
        publication_id = _validate_context(context, self.configuration.sessions)
        _publish_progress(self.progress, "loading_context", "completed", (1, 1))
        if self.cancel():
            raise KeyboardInterrupt
        if (
            not previous.issubset(item.code for item in context.universe)
            and HISTORY_UNIVERSE_CONTRACT not in context.source_versions.dependency_versions
        ):
            raise RuntimeError("supplier_universe_regressed")
        if pending is not None:
            return self._resume(context, current_snapshot, pending, publication_id)
        source, calendar, universe = _control_identities(context)
        reference = HistoryReferenceSnapshot(
            tuple(item.code for item in context.universe), context.industry_intervals, context.st_evidence
        )
        tasks = _download_tasks(context, current_snapshot, previous, self.index, self.configuration.reread_sessions)
        if self.repair_only:
            gaps = self.index.gaps_by_code()
            tasks = tuple(HistoryDownloadTask(item.code, gaps.get(item.code, ()), tencent_done=True) for item in context.universe)
        if (
            current_snapshot
            and not any(task.dates for task in tasks)
            and current_snapshot.source_identity_hash == source.content_hash
        ):
            self._repair_reference(source, reference, publication_id)
            return None
        self.control.save_inputs((source, calendar, universe), reference, publication_id)
        batch = HistoryDownloadBatch(
            current_snapshot.sequence + 1 if current_snapshot else 1,
            source.content_hash,
            calendar.content_hash,
            universe.content_hash,
            calendar.open_dates[-1],
        )
        self.index.begin(batch, tasks)
        return _BatchInput(context, source, calendar, universe, reference, batch, current_snapshot is None)

    def _resume(
        self,
        context: HistorySupplierContext,
        current_snapshot: HistorySnapshot | None,
        pending: HistoryDownloadBatch,
        publication_id: str,
    ) -> _BatchInput:
        state = self.control.load_state()
        source = next((item for item in state.sources if item.content_hash == pending.source_hash), None)
        calendar = next((item for item in state.calendars if item.content_hash == pending.calendar_hash), None)
        universe = next((item for item in state.universes if item.content_hash == pending.universe_hash), None)
        if (
            source is None
            or calendar is None
            or universe is None
            or pending.sequence != (current_snapshot.sequence + 1 if current_snapshot else 1)
        ):
            raise RuntimeError("history_download_identity_conflict")
        current_source, current_calendar, current_universe = _control_identities(context)
        if calendar.open_dates != context.calendar.open_dates:
            raise RuntimeError("history_resume_calendar_changed")
        if pending.cutoff != calendar.open_dates[-1]:
            raise RuntimeError("history_resume_cutoff_changed")
        previous_by_code = {item.code: item for item in universe.securities}
        for current_security in current_universe.securities:
            if IsStSecurityName(current_security.name):
                raise RuntimeError("history_resume_st_conflict")
            previous_security = previous_by_code.get(current_security.code)
            if previous_security is None:
                continue
            if IsStSecurityName(previous_security.name):
                raise RuntimeError("history_resume_st_conflict")
            if replace(previous_security, name=current_security.name) != current_security:
                raise RuntimeError("history_resume_security_changed")
        if (source.source, source.dataset) != (current_source.source, current_source.dataset):
            raise RuntimeError("history_resume_source_changed")
        reference = ReadBoundHistoryReference(self.configuration.history_root, source.supplier_contract, sqlite_policy=self.sqlite_policy)
        if (
            source.supplier_contract.rsplit("_", 1)[-1] != current_source.supplier_contract.rsplit("_", 1)[-1]
            or universe.securities != current_universe.securities
        ):
            # Bind the current reference while retaining progress only for common codes.
            # Persist immutable parents before atomically switching the unpublished batch.
            reference = HistoryReferenceSnapshot(
                tuple(item.code for item in context.universe), context.industry_intervals, context.st_evidence
            )
            self.control.save_inputs((current_source, current_calendar, current_universe), reference, publication_id)
            current = HistoryDownloadBatch(
                pending.sequence,
                current_source.content_hash,
                current_calendar.content_hash,
                current_universe.content_hash,
                pending.cutoff,
            )
            if previous_by_code.keys() != {item.code for item in current_universe.securities}:
                self.index.rebind_context(
                    pending,
                    current,
                    tasks=tuple(
                        HistoryDownloadTask(item.code, context.calendar.expected_dates(item))
                        for item in context.universe
                    ),
                )
            else:
                self.index.rebind_context(pending, current)
            return _BatchInput(context, current_source, current_calendar, current_universe, reference, current, False)
        return _BatchInput(context, source, calendar, universe, reference, pending, False)

    def _repair_reference(
        self, source: HistorySourceIdentity, reference: HistoryReferenceSnapshot, publication_id: str
    ) -> None:
        try:
            ReadBoundHistoryReference(self.configuration.history_root, source.supplier_contract, sqlite_policy=self.sqlite_policy)
        except (OSError, ValueError):
            self.control.save_reference(reference, publication_id)

    def _download(self, inputs: _BatchInput, current_snapshot: HistorySnapshot | None) -> HistorySnapshot:
        root = self.configuration.history_root
        activity = HistorySqliteActivity(
            lambda: _publish_progress(self.progress, "persisting_partitions", "waiting"),
            self.cancel,
        )
        prices = HistoryDirectPrices(root, inputs.batch.sequence, current_snapshot, activity, fresh=inputs.fresh, sqlite_policy=self.sqlite_policy)
        repair = HistoryPriceRepair(root, sqlite_policy=self.sqlite_policy)
        repair.replay(inputs.batch.sequence)
        flow = HistoryDownloadFlow(
            HistoryDownloadResources(
                self.configuration,
                inputs.context,
                self.supplier,
                prices,
                self.index,
                self.executor,
                self.writer,
                self.write_tuning
                or ChooseHistoryWriteTuning(
                    self.storage_kind,
                    self.configuration.write_batches,
                    fresh=current_snapshot is None,
                ),
                self.cancel,
                self.progress,
            ), sqlite_policy=self.sqlite_policy
        )
        if not self.repair_only:
            flow.tencent()
        prices.prepare_retry_pass()
        gaps = self.index.gaps_by_code()
        batch_id = uuid4().hex
        selected = tuple(item for item in inputs.context.universe if item.code in gaps)
        attempted = repaired = 0
        _publish_progress(self.progress, "baostock_repair", "started", (0, len(selected)))
        for completed, security in enumerate(selected, 1):
            if self.cancel():
                raise KeyboardInterrupt
            dates = inputs.context.calendar.expected_dates(security)
            eligible = tuple(day for day in gaps[security.code] if day in dates)
            if not repair.reserve(security.code, eligible, batch_id, explicit=self.repair_only):
                continue
            attempted += 1
            try:
                candidate = None
                if repair.source(security.code, inputs.batch.sequence) == "baostock":
                    previous = PublishedHistoryWindow(security.code, tuple(ProjectHistoryCell(cell) for cell in prices.read(security.code, dates)))
                    obtained = {cell.trade_date for cell in previous.cells if cell.status in ("complete", "supplier_marked_suspended")}
                    missing = tuple(day for day in dates if day not in obtained)
                    if len(missing) <= 5:
                        tail = self.supplier.fetch_baostock_window(security, dates[-max(5, len(missing) + 3):])
                        candidate = MergeDailyTail(previous, tail, dates)
                if candidate is None:
                    candidate = self.supplier.fetch_baostock_window(security, dates)
                repair.replace(security, dates, candidate, prices, inputs.batch.sequence)
            except (OSError, RuntimeError, ValueError) as exc:
                repair.fail(security.code, batch_id, DownloadFailure(exc))
                if self.progress is not None:
                    self.progress.publish(HistorySyncProgress("baostock_repair", "failed", completed, len(selected), security.code, failure_reason=DownloadFailure(exc)))
            else:
                repaired += 1
                _publish_progress(self.progress, "baostock_repair", "completed", (completed, len(selected)), security.code)
        repair.replay(inputs.batch.sequence)
        if self.progress is not None:
            self.progress.publish(HistorySyncProgress("baostock_repair", "completed", len(selected), len(selected), repair_summary=HistoryRepairSummary(attempted, repaired, len(selected) - attempted)))
        remaining_gaps = self.index.gaps_by_code()
        for security in inputs.context.universe:
            if missing_dates := remaining_gaps.get(security.code):
                prices.record_missing(security, missing_dates)
        if self.cancel():
            raise KeyboardInterrupt
        partitions = prices.publishable_partitions(
            inputs.calendar.open_dates,
            lambda state, done, total, month: _publish_progress(
                self.progress, "sealing_partitions", state, (done, total), month
            ),
        )
        calendar, batch = inputs.calendar, inputs.batch
        snapshot = HistorySnapshot(
            batch.sequence,
            batch.cutoff,
            calendar.open_dates[-2] if len(calendar.open_dates) > 1 else batch.cutoff,
            calendar.content_hash,
            inputs.universe.content_hash,
            inputs.source.content_hash,
            partitions,
        )
        total = len(inputs.context.universe)
        _publish_progress(self.progress, "publishing_snapshot", "started")
        PublishHistorySnapshot(
            self.control,
            HistoryPublication(
                snapshot,
                HistorySyncCheckpoint(
                    f"direct-{batch.sequence}-{batch.cutoff.isoformat()}",
                    1,
                    "completed",
                    self.observed_at,
                    total,
                    total,
                    None,
                ),
                inputs.reference,
            ),
        )
        _publish_progress(self.progress, "publishing_snapshot", "completed", (1, 1))
        return snapshot



def RunHistorySync(  # noqa: PLR0913 - explicit external resource injection preserves the public entrypoint
    configuration: HistorySyncConfiguration,
    supplier: HistoryTwoStageSupplier,
    *,
    clock: Clock | None = None,
    cancel_requested: Cancellation | None = None,
    progress: HistorySyncProgressPort | None = None,
    worker_pool: WorkerExecutor | None = None,
    writer_pool: WorkerExecutor | None = None,
    write_tuning: HistoryWriteTuning | None = None,
    storage_kind: StorageKind = "unknown",
    repair_only: bool = False, sqlite_policy: SqliteIoPolicy,
) -> HistoryMaintenanceStatus:
    observed_at = (clock or (lambda: datetime.now(_SHANGHAI)))()
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("history synchronization clock must be timezone-aware")
    observed_at = observed_at.astimezone(_SHANGHAI)
    cancel = cancel_requested or (lambda: False)
    control = HistoryControlIndex(
        configuration.history_root / "catalog.sqlite3",
        reference_path=configuration.reference_root / "company_st.sqlite3", sqlite_policy=sqlite_policy,
    )
    index = HistoryDownloadIndex(configuration.history_root / "catalog.sqlite3", sqlite_policy=sqlite_policy)
    _publish_progress(progress, "initializing", "started")
    try:
        with HistoryMaintenanceLock(configuration.history_root.with_name(".history.lock")):
            _publish_progress(progress, "storage_profile", "completed", current_item=storage_kind)
            if (configuration.history_root / "control.sqlite3").exists() or (
                not (configuration.history_root / "catalog.sqlite3").exists()
                and (
                    (configuration.history_root / "partitions").exists()
                    or any(configuration.history_root.glob("prices_*.sqlite3"))
                )
            ):
                raise RuntimeError("history_format_requires_rebuild")
            control.initialize()
            HistoryPriceRepair(configuration.history_root, sqlite_policy=sqlite_policy).initialize()
            return _HistorySync(
                configuration,
                supplier,
                observed_at,
                cancel,
                progress,
                worker_pool or InjectedExecutor(None),
                writer_pool or InjectedExecutor(None),
                control,
                index,
                storage_kind,
                write_tuning,
                repair_only, sqlite_policy=sqlite_policy,
            ).execute()
    except KeyboardInterrupt:
        return _status("cancelled", "cancelled", configuration, _safe_snapshot(control))
    except HistoryMaintenanceAlreadyRunningError:
        return _status("already_running", "history_maintenance_running", configuration, _safe_snapshot(control))
    except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
        reason = HistoryFailureCode(exc)
        detail = HistoryFailureDetail(exc)
        if progress is not None:
            try:
                progress.publish(
                    HistorySyncProgress(
                        exc.stage if isinstance(exc, HistorySyncFailureError) else "synchronization",
                        "failed",
                        0,
                        1,
                        exc.item if isinstance(exc, HistorySyncFailureError) else None,
                        failure_reason=reason,
                        failure_detail=detail,
                    )
                )
            except OSError:
                pass
        else:
            _LOGGER.error("history_sync_failure reason=%s diagnostic=%s", reason, detail)
        return _status("failed", reason, configuration, _safe_snapshot(control))


def _download_tasks(
    context: HistorySupplierContext,
    current_snapshot: HistorySnapshot | None,
    previous: frozenset[str],
    index: HistoryDownloadIndex,
    reread: int,
) -> tuple[HistoryDownloadTask, ...]:
    retryable = index.gaps_by_code(context.calendar.open_dates)
    tasks = []
    for security in context.universe:
        expected = context.calendar.expected_dates(security)
        if current_snapshot is None or security.code not in previous:
            dates = expected
        else:
            missing = {day for day in retryable.get(security.code, ()) if day <= context.calendar.open_dates[-1]}
            new = {day for day in expected if day > current_snapshot.data_cutoff}
            anchors = (
                tuple(day for day in expected if day <= current_snapshot.data_cutoff)[-reread:]
                if new or missing
                else ()
            )
            dates = tuple(sorted(missing | new | set(anchors)))
        tasks.append(HistoryDownloadTask(security.code, dates))
    return tuple(tasks)


def _has_sufficient_disk(root: Path, minimum_free_bytes: int) -> bool:
    return InspectHistoryDisk(root, HistoryDiskRequirement(minimum_free_bytes, 0, 0, 0)).sufficient


def _control_identities(
    context: HistorySupplierContext,
) -> tuple[HistorySourceIdentity, HistoryCalendarIdentity, HistoryUniverseIdentity]:
    cutoff = context.calendar.open_dates[-1]
    observed = datetime.combine(cutoff, _BAOSTOCK_DAILY_READY, _SHANGHAI)
    reference = HistoryReferenceSnapshot(
        tuple(item.code for item in context.universe), context.industry_intervals, context.st_evidence
    )
    supplier_contract = f"history_ref_{reference.content_hash}_{CanonicalDigest(context.source_versions)[:16]}"
    source = HistorySourceIdentity("tencent_baostock", "history_daily", supplier_contract[:128], observed)
    calendar = HistoryCalendarIdentity(context.calendar.open_dates, source.content_hash)
    universe = HistoryUniverseIdentity(
        tuple(
            HistorySecurityIdentity(item.code, item.name, item.board, item.listed_on, item.delisted_on)
            for item in context.universe
        ),
        source.content_hash,
    )
    return source, calendar, universe


def _validate_context(context: HistorySupplierContext, sessions: int) -> str:
    if len(context.calendar.open_dates) != sessions:
        raise RuntimeError("supplier_calendar_incomplete")
    if context.reference_id is None:
        raise RuntimeError("history_reference_publication_missing")
    return context.reference_id


def _safe_snapshot(control: HistoryControlIndex) -> HistorySnapshot | None:
    try:
        state = control.load_published_state()
        return state.snapshot if state is not None else None
    except HistoryControlError:
        return None


def _publish_progress(  # noqa: PLR0913 - typed progress projection, optional preparation capacity
    progress: HistorySyncProgressPort | None,
    stage: HistorySyncProgressStage,
    state: Literal["started", "waiting", "retrying", "completed", "failed", "cancelled"],
    counts: tuple[int, int] = (0, 1),
    current_item: str | None = None,
    *,
    required_free_bytes: int | None = None,
) -> None:
    if progress is None:
        return
    try:
        progress.publish(
            HistorySyncProgress(stage, state, *counts, current_item, required_free_bytes=required_free_bytes)
        )
    except OSError:
        pass


def _status(  # noqa: PLR0913 - immutable status projection keeps the public fields explicit
    state: HistoryMaintenanceState,
    reason: str | None,
    configuration: HistorySyncConfiguration,
    snapshot: HistorySnapshot | None,
    *,
    unresolved_price_cells: int = 0,
    price_gap_summary: HistoryPriceGapSummary | None = None,
) -> HistoryMaintenanceStatus:
    root = configuration.history_root
    return HistoryMaintenanceStatus(
        state=state,
        reason=reason,
        history_root=root,
        selected_baseline_source="tencent_then_baostock_repair",
        efficient_daily_source="tencent",
        snapshot_hash=snapshot.content_hash if snapshot is not None else None,
        data_cutoff=snapshot.data_cutoff if snapshot is not None else None,
        label_cutoff=snapshot.label_cutoff if snapshot is not None else None,
        unresolved_price_cells=unresolved_price_cells,
        price_gap_summary=price_gap_summary,
    )


__all__ = ["RunHistorySync"]
