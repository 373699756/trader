"""Direct monthly history downloads with durable metadata and logical snapshot publication."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
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
from trader.download.domain.history_maintenance import HistoryMaintenanceState, HistoryMaintenanceStatus
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
from trader.download.infra.history_control_index import (
    HistoryControlError,
    HistoryControlIndex,
    HistoryMaintenanceAlreadyRunningError,
    HistoryMaintenanceLock,
    inspect_history_disk,
)
from trader.download.infra.history_direct_prices import HistoryDirectPrices
from trader.download.infra.history_download_flow import HistoryDownloadFlow, HistoryDownloadResources
from trader.download.infra.history_download_index import HistoryDownloadBatch, HistoryDownloadIndex, HistoryDownloadTask
from trader.download.infra.history_reference_files import (
    history_reference_path,
    read_bound_history_reference,
    write_history_reference,
)
from trader.download.infra.history_snapshot_publication import HistoryPublication, publish_history_snapshot
from trader.download.infra.history_sqlite_activity import HistorySqliteActivity
from trader.infra.workers import WorkerExecutor, injected_executor
from trader.training.domain.evaluation.artifact_identity import canonical_artifact_hash
from trader.training.infra.history.history_training_due import HistoryTrainingDueQuery, evaluate_history_training_due
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE

Clock: TypeAlias = Callable[[], datetime]
Cancellation: TypeAlias = Callable[[], bool]
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_BAOSTOCK_DAILY_READY = time(15, 10)
_ERROR_CODE = re.compile(r"[a-z0-9_]{1,64}")


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
                return _status(
                    "completed" if published else "already_current", None, self.configuration, active, self.observed_at
                )
            snapshot = self._download(inputs, active)
            published = True
            if pending is not None and target < _latest_completed_daily_date(self.observed_at):
                continue
            unresolved = self.index.unresolved(tuple(item.code for item in inputs.context.universe))
            return _status(
                "completed",
                "history_price_gaps_remaining" if unresolved else None,
                self.configuration,
                snapshot,
                self.observed_at,
                unresolved_price_cells=unresolved,
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
        write_history_reference(
            history_reference_path(self.configuration.archive_root, reference.content_hash), reference
        )
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
        current_source, _, current_universe = _control_identities(context)
        if (
            calendar.open_dates != context.calendar.open_dates
            or universe.securities != current_universe.securities
            or source.supplier_contract.rsplit("_", 1)[-1] != current_source.supplier_contract.rsplit("_", 1)[-1]
        ):
            raise RuntimeError("history_resume_context_changed")
        reference = read_bound_history_reference(self.configuration.archive_root, source.supplier_contract)
        return _BatchInput(context, source, calendar, universe, reference, pending, False)

    def _repair_reference(self, source: HistorySourceIdentity, reference: HistoryReferenceSnapshot) -> None:
        try:
            read_bound_history_reference(self.configuration.archive_root, source.supplier_contract)
        except (OSError, ValueError):
            write_history_reference(
                history_reference_path(self.configuration.archive_root, reference.content_hash), reference
            )

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
                self.cancel,
                self.progress,
            )
        )
        flow.tencent()
        flow.baostock()
        for security in inputs.context.universe:
            if gaps := self.index.gaps(security.code):
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
        publish_history_snapshot(
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


def run_history_sync(  # noqa: PLR0913 - explicit external resource injection preserves the public entrypoint
    configuration: HistorySyncConfiguration,
    supplier: HistoryTwoStageSupplier,
    *,
    clock: Clock | None = None,
    cancel_requested: Cancellation | None = None,
    progress: HistorySyncProgressPort | None = None,
    worker_pool: WorkerExecutor | None = None,
    writer_pool: WorkerExecutor | None = None,
) -> HistoryMaintenanceStatus:
    observed_at = (clock or (lambda: datetime.now(_SHANGHAI)))()
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("history synchronization clock must be timezone-aware")
    observed_at = observed_at.astimezone(_SHANGHAI)
    cancel = cancel_requested or (lambda: False)
    control = HistoryControlIndex(configuration.archive_root / "control.sqlite3")
    index = HistoryDownloadIndex(configuration.archive_root / "control.sqlite3")
    _publish_progress(progress, "initializing", "started")
    try:
        with HistoryMaintenanceLock(configuration.archive_root / ".maintenance.lock"):
            if not (configuration.archive_root / "control.sqlite3").exists() and any(
                (configuration.archive_root / "partitions").glob("*/*.sqlite3")
            ):
                raise RuntimeError("history_format_requires_rebuild")
            control.initialize()
            index.initialize()
            return _HistorySync(
                configuration,
                supplier,
                observed_at,
                cancel,
                progress,
                worker_pool or injected_executor(None),
                writer_pool or injected_executor(None),
                control,
                index,
            ).execute()
    except KeyboardInterrupt:
        return _status("cancelled", "cancelled", configuration, _safe_active(control), observed_at)
    except HistoryMaintenanceAlreadyRunningError:
        return _status(
            "already_running", "history_maintenance_running", configuration, _safe_active(control), observed_at
        )
    except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
        return _status("failed", _failure_code(exc), configuration, _safe_active(control), observed_at)


def _download_tasks(
    context: HistorySupplierContext,
    control: HistoryControlIndex,
    active: HistoryActiveSnapshot | None,
    index: HistoryDownloadIndex,
    reread: int,
) -> tuple[HistoryDownloadTask, ...]:
    previous = _active_universe(control, active)
    tasks = []
    for security in context.universe:
        expected = context.calendar.expected_dates(security)
        if active is None or security.code not in previous:
            dates = expected
        else:
            missing = {day for day in index.gaps(security.code) if day <= context.calendar.open_dates[-1]}
            new = {day for day in expected if day > active.data_cutoff}
            anchors = tuple(day for day in expected if day <= active.data_cutoff)[-reread:] if new or missing else ()
            dates = tuple(sorted(missing | new | set(anchors)))
        tasks.append(HistoryDownloadTask(security.code, dates))
    return tuple(tasks)


def _has_sufficient_disk(root: Path, minimum_free_bytes: int) -> bool:
    return inspect_history_disk(root, HistoryDiskRequirement(minimum_free_bytes, 0, 0, 0)).sufficient


def _control_identities(
    context: HistorySupplierContext,
) -> tuple[HistorySourceIdentity, HistoryCalendarIdentity, HistoryUniverseIdentity]:
    cutoff = context.calendar.open_dates[-1]
    observed = datetime.combine(cutoff, _BAOSTOCK_DAILY_READY, _SHANGHAI)
    reference = HistoryReferenceSnapshot(
        tuple(item.code for item in context.universe), context.industry_intervals, context.st_evidence
    )
    supplier_contract = f"history_ref_{reference.content_hash}_{canonical_artifact_hash(context.source_versions)[:16]}"
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


def _failure_code(exc: BaseException) -> str:
    value = str(exc).strip()
    return value if _ERROR_CODE.fullmatch(value) else "history_sync_failed"


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
) -> HistoryMaintenanceStatus:
    root = configuration.archive_root
    due = None
    if snapshot is not None:
        due = evaluate_history_training_due(
            HistoryTrainingDueQuery(root, configuration.training_root / "v3", observed_at, V3_TRAINING_PROFILE)
        )
    due_state = (
        due.state
        if due is not None and snapshot is not None and due.active_snapshot.content_hash == snapshot.content_hash
        else None
    )
    return HistoryMaintenanceStatus(
        state=state,
        reason=reason,
        archive_root=root,
        selected_baseline_source="tencent_then_baostock_gap",
        efficient_daily_source="tencent",
        active_snapshot_hash=snapshot.content_hash if snapshot is not None else None,
        data_cutoff=snapshot.data_cutoff if snapshot is not None else None,
        label_cutoff=snapshot.label_cutoff if snapshot is not None else None,
        matured_label_days_since_training=(due_state.matured_label_days_since_training if due_state else 0),
        training_due=(due_state.training_due if due_state else False),
        training_due_reason=(due_state.reason if due_state else "data_incomplete"),
        automatic_training=False,
        unresolved_price_cells=unresolved_price_cells,
    )


__all__ = ["run_history_sync"]
