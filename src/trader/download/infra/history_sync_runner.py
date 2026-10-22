"""Direct monthly history downloads with durable metadata and logical snapshot publication."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Literal, TypeAlias
from zoneinfo import ZoneInfo

from trader.download.domain.history_control import (
    HistoryActiveSnapshot,
    HistoryCalendarIdentity,
    HistoryDiskRequirement,
    HistorySecurityIdentity,
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
    HistorySyncConfiguration,
    HistorySyncProgress,
    HistorySyncProgressPort,
    HistorySyncProgressStage,
    HistoryTwoStageSupplier,
)
from trader.download.domain.security_eligibility import IsStSecurityName
from trader.download.infra.history_control_index import (
    HistoryControlError,
    HistoryControlIndex,
    HistoryMaintenanceAlreadyRunningError,
    HistoryMaintenanceLock,
    InspectHistoryDisk,
)
from trader.download.infra.history_direct_prices import HistoryDirectPrices
from trader.download.infra.history_download_flow import HistoryDownloadFlow, HistoryDownloadResources
from trader.download.infra.history_download_index import HistoryDownloadBatch, HistoryDownloadIndex, HistoryDownloadTask
from trader.download.infra.history_reference_codec import (
    ReadBoundHistoryReference,
)
from trader.download.infra.history_snapshot_publication import HistoryPublication, PublishHistorySnapshot
from trader.download.infra.history_sqlite_activity import HistorySqliteActivity
from trader.download.infra.history_storage_profile import (
    ChooseHistoryWriteTuning,
    ClassifyHistoryStorage,
    HistoryStorageKind,
    HistoryWriteTuning,
)
from trader.download.infra.history_sync_failure import (
    HistoryFailureCode,
    HistoryFailureDetail,
    HistorySyncFailureError,
)
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.workers import InjectedExecutor, WorkerExecutor
from trader.training.infra.history.history_training_due import EvaluateAllHistoryTrainingDue

Clock: TypeAlias = Callable[[], datetime]
Cancellation: TypeAlias = Callable[[], bool]
_SHANGHAI = ZoneInfo("Asia/Shanghai")
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
    storage_kind: HistoryStorageKind
    write_tuning: HistoryWriteTuning | None

    def execute(self) -> HistoryMaintenanceStatus:
        published = False
        while True:
            active = self.control.load_state().active_snapshot
            if not _has_sufficient_disk(self.configuration.archive_root, self.configuration.minimum_free_bytes):
                return _status("blocked", "disk_space_insufficient", self.configuration, active, self.observed_at)
            pending = self.index.pending(active.sequence if active else 0)
            target = pending.cutoff if pending else _latest_completed_daily_date(self.observed_at)
            inputs = self._prepare(active, pending, target)
            if inputs is None:
                unresolved = (
                    self.index.gap_summary(tuple(_active_universe(self.control, active)))
                    if active is not None
                    else None
                )
                return _status(
                    "completed" if published else "already_current",
                    "history_price_gaps_remaining" if unresolved and unresolved.cells else None,
                    self.configuration,
                    active,
                    self.observed_at,
                    unresolved_price_cells=unresolved.cells if unresolved else 0,
                    price_gap_summary=unresolved,
                )
            snapshot = self._download(inputs, active)
            published = True
            if pending is not None and target < _latest_completed_daily_date(self.observed_at):
                continue
            unresolved = self.index.gap_summary(tuple(item.code for item in inputs.context.universe))
            return _status(
                "completed",
                "history_price_gaps_remaining" if unresolved and unresolved.cells else None,
                self.configuration,
                snapshot,
                self.observed_at,
                unresolved_price_cells=unresolved.cells if unresolved else 0,
                price_gap_summary=unresolved,
            )

    def _prepare(
        self,
        active: HistoryActiveSnapshot | None,
        pending: HistoryDownloadBatch | None,
        target: date,
    ) -> _BatchInput | None:
        _publish_progress(self.progress, "loading_context", "started")
        context = self.supplier.load_context(target, self.configuration.sessions)
        _validate_context(context, self.configuration.sessions)
        _publish_progress(self.progress, "loading_context", "completed", (1, 1))
        if self.cancel():
            raise KeyboardInterrupt
        if (
            not _active_universe(self.control, active).issubset(item.code for item in context.universe)
            and HISTORY_UNIVERSE_CONTRACT not in context.source_versions.dependency_versions
        ):
            raise RuntimeError("supplier_universe_regressed")
        if pending is not None:
            return self._resume(context, active, pending)
        source, calendar, universe = _control_identities(context)
        reference = HistoryReferenceSnapshot(
            tuple(item.code for item in context.universe), context.industry_intervals, context.st_evidence
        )
        tasks = _download_tasks(context, self.control, active, self.index, self.configuration.reread_sessions)
        if active and not any(task.dates for task in tasks) and active.source_identity_hash == source.content_hash:
            self._repair_reference(source, reference)
            return None
        self.control.save_source(source)
        self.control.save_calendar(calendar)
        self.control.save_universe(universe)
        self.control.save_reference(reference)
        batch = HistoryDownloadBatch(
            active.sequence + 1 if active else 1,
            source.content_hash,
            calendar.content_hash,
            universe.content_hash,
            calendar.open_dates[-1],
        )
        self.index.begin(batch, tasks)
        return _BatchInput(context, source, calendar, universe, reference, batch, active is None)

    def _resume(
        self,
        context: HistorySupplierContext,
        active: HistoryActiveSnapshot | None,
        pending: HistoryDownloadBatch,
    ) -> _BatchInput:
        state = self.control.load_state()
        source = next((item for item in state.sources if item.content_hash == pending.source_hash), None)
        calendar = next((item for item in state.calendars if item.content_hash == pending.calendar_hash), None)
        universe = next((item for item in state.universes if item.content_hash == pending.universe_hash), None)
        if (
            source is None
            or calendar is None
            or universe is None
            or pending.sequence != (active.sequence + 1 if active else 1)
        ):
            raise RuntimeError("history_download_identity_conflict")
        current_source, current_calendar, current_universe = _control_identities(context)
        if calendar.open_dates != context.calendar.open_dates:
            raise RuntimeError("history_resume_calendar_changed")
        if pending.cutoff != calendar.open_dates[-1]:
            raise RuntimeError("history_resume_cutoff_changed")
        previous_securities = {item.code: item for item in universe.securities}
        current_securities = {item.code: item for item in current_universe.securities}
        tasks = self.index.tasks()
        if {task.code for task in tasks} != set(previous_securities):
            raise RuntimeError("history_resume_task_population_conflict")
        for task in tasks:
            security = previous_securities[task.code]
            expected_dates = {
                day
                for day in calendar.open_dates
                if day >= security.listed_on and (security.delisted_on is None or day < security.delisted_on)
            }
            if not set(task.dates).issubset(expected_dates) or (
                active is None and task.dates != tuple(sorted(expected_dates))
            ):
                raise RuntimeError("history_resume_task_dates_conflict")
        if any(IsStSecurityName(item.name) for item in previous_securities.values()) or any(
            IsStSecurityName(item.name) for item in current_securities.values()
        ):
            raise RuntimeError("history_resume_st_conflict")
        for code in previous_securities.keys() & current_securities.keys():
            previous_security = previous_securities[code]
            current_security = current_securities[code]
            if replace(previous_security, name=current_security.name) != current_security:
                raise RuntimeError("history_resume_security_changed")
        if (source.source, source.dataset) != (current_source.source, current_source.dataset):
            raise RuntimeError("history_resume_source_changed")
        reference = ReadBoundHistoryReference(self.configuration.archive_root, source.supplier_contract)
        if (
            source.supplier_contract.rsplit("_", 1)[-1] != current_source.supplier_contract.rsplit("_", 1)[-1]
            or universe.securities != current_universe.securities
        ):
            added_codes = current_securities.keys() - previous_securities.keys()
            removed_codes = previous_securities.keys() - current_securities.keys()
            planned_tasks = _download_tasks(
                context, self.control, active, self.index, self.configuration.reread_sessions
            )
            reference = HistoryReferenceSnapshot(
                tuple(item.code for item in context.universe), context.industry_intervals, context.st_evidence
            )
            self.control.save_source(current_source)
            self.control.save_calendar(current_calendar)
            self.control.save_universe(current_universe)
            self.control.save_reference(reference)
            current = HistoryDownloadBatch(
                pending.sequence,
                current_source.content_hash,
                current_calendar.content_hash,
                current_universe.content_hash,
                pending.cutoff,
            )
            self.index.rebind_context(
                pending,
                current,
                removed_codes=frozenset(removed_codes),
                added_tasks=tuple(task for task in planned_tasks if task.code in added_codes),
            )
            return _BatchInput(context, current_source, current_calendar, current_universe, reference, current, False)
        return _BatchInput(context, source, calendar, universe, reference, pending, False)

    def _repair_reference(self, source: HistorySourceIdentity, reference: HistoryReferenceSnapshot) -> None:
        try:
            ReadBoundHistoryReference(self.configuration.archive_root, source.supplier_contract)
        except (OSError, ValueError):
            self.control.save_reference(reference)

    def _download(self, inputs: _BatchInput, active: HistoryActiveSnapshot | None) -> HistoryActiveSnapshot:
        root = self.configuration.archive_root
        activity = HistorySqliteActivity(
            lambda: _publish_progress(self.progress, "persisting_partitions", "waiting"),
            self.cancel,
        )
        prices = HistoryDirectPrices(root, inputs.batch.sequence, active, activity, fresh=inputs.fresh)
        flow = HistoryDownloadFlow(
            HistoryDownloadResources(
                self.configuration,
                inputs.context,
                self.supplier,
                prices,
                self.index,
                self.executor,
                self.writer,
                self.write_tuning or ChooseHistoryWriteTuning(self.storage_kind, fresh=inputs.fresh),
                self.cancel,
                self.progress,
            )
        )
        flow.tencent()
        remaining_gaps = self.index.gaps_by_code()
        for security in inputs.context.universe:
            if gaps := remaining_gaps.get(security.code):
                prices.record_missing(security, gaps)
        if self.cancel():
            raise KeyboardInterrupt
        partitions = prices.publishable_partitions(
            inputs.calendar.open_dates,
            lambda done, total, month: _publish_progress(
                self.progress, "sealing_partitions", "waiting", (done, total), month
            ),
        )
        calendar, batch = inputs.calendar, inputs.batch
        snapshot = HistoryActiveSnapshot(
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
                root,
                (inputs.source, calendar, inputs.universe),
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
) -> HistoryMaintenanceStatus:
    observed_at = (clock or (lambda: datetime.now(_SHANGHAI)))()
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("history synchronization clock must be timezone-aware")
    observed_at = observed_at.astimezone(_SHANGHAI)
    cancel = cancel_requested or (lambda: False)
    control = HistoryControlIndex(configuration.archive_root / "catalog.sqlite3")
    index = HistoryDownloadIndex(configuration.archive_root / "catalog.sqlite3")
    _publish_progress(progress, "initializing", "started")
    try:
        with HistoryMaintenanceLock(configuration.archive_root.with_name(".history.lock")):
            storage_kind = ClassifyHistoryStorage(configuration.archive_root)
            _publish_progress(progress, "storage_profile", "completed", current_item=storage_kind)
            if (configuration.archive_root / "control.sqlite3").exists() or (
                not (configuration.archive_root / "catalog.sqlite3").exists()
                and (
                    (configuration.archive_root / "partitions").exists()
                    or any(configuration.archive_root.glob("prices_*.sqlite3"))
                )
            ):
                raise RuntimeError("history_format_requires_rebuild")
            control.initialize()
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
            ).execute()
    except KeyboardInterrupt:
        return _status("cancelled", "cancelled", configuration, _safe_active(control), observed_at)
    except HistoryMaintenanceAlreadyRunningError:
        return _status(
            "already_running", "history_maintenance_running", configuration, _safe_active(control), observed_at
        )
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
        return _status("failed", reason, configuration, _safe_active(control), observed_at)


def _download_tasks(
    context: HistorySupplierContext,
    control: HistoryControlIndex,
    active: HistoryActiveSnapshot | None,
    index: HistoryDownloadIndex,
    reread: int,
) -> tuple[HistoryDownloadTask, ...]:
    previous = _active_universe(control, active)
    retryable = index.gaps_by_code(context.calendar.open_dates)
    tasks = []
    for security in context.universe:
        expected = context.calendar.expected_dates(security)
        if active is None or security.code not in previous:
            dates = expected
        else:
            missing = {day for day in retryable.get(security.code, ()) if day <= context.calendar.open_dates[-1]}
            new = {day for day in expected if day > active.data_cutoff}
            anchors = tuple(day for day in expected if day <= active.data_cutoff)[-reread:] if new or missing else ()
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


def _validate_context(context: HistorySupplierContext, sessions: int) -> None:
    if len(context.calendar.open_dates) != sessions:
        raise RuntimeError("supplier_calendar_incomplete")


def _is_current(
    active: HistoryActiveSnapshot | None,
    calendar: HistoryCalendarIdentity,
    universe: HistoryUniverseIdentity,
) -> bool:
    return bool(
        active is not None
        and active.data_cutoff == calendar.open_dates[-1]
        and active.calendar_hash == calendar.content_hash
        and active.universe_hash == universe.content_hash
    )


def _active_universe(control: HistoryControlIndex, active: HistoryActiveSnapshot | None) -> frozenset[str]:
    if active is None:
        return frozenset()
    state = control.load_state()
    universe = next(item for item in state.universes if item.content_hash == active.universe_hash)
    return frozenset(item.code for item in universe.securities)


def _latest_completed_daily_date(observed_at: datetime) -> date:
    if observed_at.time() >= _BAOSTOCK_DAILY_READY:
        return observed_at.date()
    return observed_at.date() - timedelta(days=1)


def _safe_active(control: HistoryControlIndex) -> HistoryActiveSnapshot | None:
    try:
        return control.load_state().active_snapshot
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
    snapshot: HistoryActiveSnapshot | None,
    observed_at: datetime,
    *,
    unresolved_price_cells: int = 0,
    price_gap_summary: HistoryPriceGapSummary | None = None,
) -> HistoryMaintenanceStatus:
    root = configuration.archive_root
    reference_ready = snapshot is not None and _has_industry_reference(root, snapshot)
    heads = EvaluateAllHistoryTrainingDue(root, configuration.training_root, observed_at) if reference_ready else ()
    states = tuple(item.due for item in heads if item.due is not None)
    complete = len(states) == 4 and all(item.reason != "data_incomplete" for item in states)
    due_state = (
        next((item for item in states if item.training_due), states[0] if states else None) if complete else None
    )
    return HistoryMaintenanceStatus(
        state=state,
        reason=reason,
        archive_root=root,
        selected_baseline_source="tencent_with_baostock_reference",
        efficient_daily_source="tencent",
        active_snapshot_hash=snapshot.content_hash if snapshot is not None else None,
        data_cutoff=snapshot.data_cutoff if snapshot is not None else None,
        label_cutoff=snapshot.label_cutoff if snapshot is not None else None,
        matured_label_days_since_training=(due_state.matured_label_days_since_training if due_state else 0),
        training_due=(due_state.training_due if due_state else False),
        training_due_reason=(due_state.reason if due_state else "data_incomplete"),
        automatic_training=False,
        unresolved_price_cells=unresolved_price_cells,
        price_gap_summary=price_gap_summary,
        training_heads=heads,
    )


def _has_industry_reference(root: Path, snapshot: HistoryActiveSnapshot) -> bool:
    try:
        source = HistoryControlIndex(root / "catalog.sqlite3").read_source(snapshot.source_identity_hash)
        return bool(ReadBoundHistoryReference(root, source.supplier_contract).industry_intervals)
    except (HistoryControlError, OSError, ValueError):
        return False


__all__ = ["RunHistorySync"]
