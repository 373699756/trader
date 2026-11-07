"""Own history planning, recovery, eligibility, repairs and publication decisions."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime

from trader.download.application.history_ports import (
    HistoryReviewBusyError, HistoryReviewDeferredError, HistorySyncAlreadyRunningError,
    HistorySyncDependencies, HistorySyncIoError,
)
from trader.download.application.history_progress import PublishHistoryProgress
from trader.download.domain.history_control import HistorySnapshot, HistorySourceIdentity, HistorySyncCheckpoint
from trader.download.domain.history_download import (
    BuildHistoryControlIdentities, HistoryBatchInput, HistoryDownloadBatch, HistoryDownloadTask,
    HistoryPublication, PlanHistoryDownloadTasks,
)
from trader.download.domain.history_maintenance import (
    HistoryMaintenanceState, HistoryMaintenanceStatus, HistoryPriceGapSummary, HistoryPriceReviewSummary,
)
from trader.download.domain.history_price_qualification import HISTORY_UNIVERSE_CONTRACT
from trader.download.domain.history_reference import HistoryReferenceSnapshot
from trader.download.domain.history_sync import (
    HistoryIndustryReadiness, HistorySupplierContext, HistoryRepairSummary, HistorySyncConfiguration,
    HistorySyncProgress, HistorySyncProgressPort, HistoryTwoStageSupplier,
)
from trader.download.domain.price_review import PriceReviewBudget, PriceReviewBudgetExhausted
from trader.download.domain.qfq_window import CompletedDailyCutoff, MergeDailyTail
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryWindow
from trader.download.domain.security_eligibility import IsStSecurityName

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _AlreadyCurrent:
    industry_readiness: HistoryIndustryReadiness | None


@dataclass(frozen=True)
class DownloadHistoryUseCase:
    configuration: HistorySyncConfiguration
    supplier: HistoryTwoStageSupplier
    observed_at: datetime
    cancel: Callable[[], bool]
    progress: HistorySyncProgressPort | None
    dependencies: HistorySyncDependencies
    repair_only: bool = False
    price_review_budget: PriceReviewBudget | None = None

    def execute(self) -> HistoryMaintenanceStatus:
        PublishHistoryProgress(self.progress, "initializing", "started")
        maintenance = self.dependencies.maintenance
        try:
            with maintenance.lease():
                result = self._synchronize()
                maintenance.finalize(result)
                return result
        except KeyboardInterrupt:
            return _status("cancelled", "cancelled", self.configuration, maintenance.safe_snapshot())
        except HistorySyncAlreadyRunningError:
            return _status("already_running", "history_maintenance_running", self.configuration, maintenance.safe_snapshot())
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            failure = exc.failure if isinstance(exc, HistorySyncIoError) else maintenance.failure(exc)
            maintenance.report_failure(failure, self.progress)
            return _status("failed", failure.reason, self.configuration, maintenance.safe_snapshot())

    def _synchronize(self) -> HistoryMaintenanceStatus:
        PublishHistoryProgress(self.progress, "initializing", "started", current_item="published_state")
        state = self.dependencies.control.load_published_state()
        current_snapshot = state.snapshot if state is not None else None
        previous = frozenset(item.code for item in state.universe.securities) if state is not None else frozenset()
        if not self.dependencies.maintenance.has_space(self.configuration.minimum_free_bytes):
            return _status("blocked", "disk_space_insufficient", self.configuration, current_snapshot)
        pending = self.dependencies.tasks.pending(current_snapshot.sequence if current_snapshot else 0)
        PublishHistoryProgress(self.progress, "initializing", "completed", (1, 1), "published_state")
        target = CompletedDailyCutoff(self.observed_at)
        if self.repair_only:
            target = pending.cutoff if pending else current_snapshot.data_cutoff if current_snapshot else target
        try:
            inputs = self._prepare(current_snapshot, pending, target, previous)
        except HistoryReviewDeferredError:
            return _status(
                "blocked",
                "history_review_deferred",
                self.configuration,
                current_snapshot,
                price_review=self._review_summary(),
            )
        if isinstance(inputs, HistoryMaintenanceStatus):
            return inputs
        if isinstance(inputs, _AlreadyCurrent):
            unresolved = self.dependencies.tasks.gap_summary(
                tuple(previous), calendar_dates=state.calendar.open_dates,
            ) if state is not None else None
            return _status(
                "already_current",
                "history_price_gaps_remaining" if unresolved and unresolved.cells else None,
                self.configuration,
                current_snapshot,
                unresolved_price_cells=unresolved.cells if unresolved else 0,
                price_gap_summary=unresolved,
                price_review=self._review_summary(),
                industry_readiness=inputs.industry_readiness,
            )
        snapshot = self._download(inputs, current_snapshot)
        unresolved = self.dependencies.tasks.gap_summary(
            tuple(item.code for item in inputs.context.universe), calendar_dates=inputs.calendar.open_dates,
        )
        return _status(
            "completed",
            "history_price_gaps_remaining" if unresolved and unresolved.cells else None,
            self.configuration,
            snapshot,
            unresolved_price_cells=unresolved.cells if unresolved else 0,
            price_gap_summary=unresolved,
            price_review=self._review_summary(),
            industry_readiness=inputs.context.industry_readiness,
        )

    def _prepare(
        self,
        current_snapshot: HistorySnapshot | None,
        pending: HistoryDownloadBatch | None,
        target: date,
        previous: frozenset[str],
    ) -> HistoryBatchInput | HistoryMaintenanceStatus | _AlreadyCurrent:
        PublishHistoryProgress(self.progress, "loading_context", "started")
        context = self.supplier.load_context(target, self.configuration.sessions)
        publication_id = _validate_context(context, self.configuration.sessions)
        PublishHistoryProgress(self.progress, "loading_context", "completed", (1, 1))
        if self.cancel():
            raise KeyboardInterrupt
        if (
            not previous.issubset(item.code for item in context.universe)
            and HISTORY_UNIVERSE_CONTRACT not in context.source_versions.dependency_versions
        ):
            raise RuntimeError("supplier_universe_regressed")
        if pending is not None:
            PublishHistoryProgress(self.progress, "initializing", "started", current_item="resuming_tasks")
            result = self._resume(context, current_snapshot, pending, publication_id)
            PublishHistoryProgress(self.progress, "initializing", "completed", (1, 1), "resuming_tasks")
            return result
        total = len(context.universe)
        PublishHistoryProgress(self.progress, "initializing", "started", (0, total), "planning_tasks")
        source, calendar, universe = BuildHistoryControlIdentities(context)
        reference = HistoryReferenceSnapshot(
            tuple(item.code for item in context.universe), context.industry_intervals, context.st_evidence
        )
        tasks = PlanHistoryDownloadTasks(
            context, current_snapshot, previous,
            self.dependencies.tasks.gaps_by_code(context.calendar.open_dates),
            self.configuration.reread_sessions,
        )
        if self.repair_only:
            gaps = self.dependencies.tasks.gaps_by_code(context.calendar.open_dates, retryable_only=False)
            tasks = tuple(HistoryDownloadTask(item.code, gaps.get(item.code, ()), tencent_done=True) for item in context.universe)
        PublishHistoryProgress(self.progress, "initializing", "completed", (total, total), "planning_tasks")
        already_current = (
            current_snapshot
            and not any(task.dates for task in tasks)
            and current_snapshot.source_identity_hash == source.content_hash
        )
        batch = HistoryDownloadBatch(
            current_snapshot.sequence + 1 if current_snapshot else 1,
            source.content_hash,
            calendar.content_hash,
            universe.content_hash,
            calendar.open_dates[-1],
        )
        inputs = HistoryBatchInput(context, source, calendar, universe, reference, batch, current_snapshot is None)
        if already_current:
            self._repair_reference(source, reference, publication_id)
            if current_snapshot is not None and self._review_current(inputs, current_snapshot, tasks, publication_id):
                return replace(inputs, reviewed=True)
            return _AlreadyCurrent(context.industry_readiness)
        PublishHistoryProgress(self.progress, "initializing", "started", (0, total), "saving_tasks")
        self.dependencies.control.save_inputs((source, calendar, universe), reference, publication_id)
        self.dependencies.tasks.begin(batch, tasks)
        PublishHistoryProgress(self.progress, "initializing", "completed", (total, total), "saving_tasks")
        return inputs

    def _review_summary(self) -> HistoryPriceReviewSummary | None:
        budget = self.price_review_budget
        if budget is None:
            return None
        remaining = self.dependencies.prices.remaining_review_codes(budget)
        return HistoryPriceReviewSummary(
            len(budget.reviewed_codes),
            budget.request_count,
            budget.exhausted_reason,
            remaining,
        )

    def _review_current(
        self, inputs: HistoryBatchInput, snapshot: HistorySnapshot,
        tasks: tuple[HistoryDownloadTask, ...], publication_id: str,
    ) -> bool:
        budget = self.price_review_budget
        if budget is None or self.repair_only:
            return False
        review = self.dependencies.prices.current_review(inputs, snapshot)
        begun = False
        try:
            with review.activity():
                prepared = review.prepare(budget)
                changed = bool(prepared.revised_dates)
                if changed:
                    budget.check_deadline()
                    self.dependencies.control.save_inputs(
                        (inputs.source, inputs.calendar, inputs.universe), inputs.reference, publication_id,
                    )
                    budget.check_deadline()
                    self.dependencies.tasks.begin(inputs.batch, tasks)
                    begun = True
                review.commit(inputs.batch.sequence if changed else snapshot.sequence, budget)
                return changed
        except PriceReviewBudgetExhausted as exc:
            if begun:
                review.seal_deferred()
                raise HistoryReviewDeferredError("history_review_deferred") from exc
            return False
        except KeyboardInterrupt:
            review.seal_deferred()
            raise
        except HistoryReviewBusyError as exc:
            if begun:
                review.seal_deferred()
                raise HistoryReviewDeferredError("history_review_deferred") from exc
            if exc.partition:
                review.seal_deferred()
                raise
            _LOGGER.warning("history_inferred_review_deferred reason=sqlite_busy")
            return False
        except (OSError, RuntimeError, TypeError, ValueError):
            review.seal_deferred()
            raise
        finally:
            budget.pause()

    def _resume(
        self,
        context: HistorySupplierContext,
        current_snapshot: HistorySnapshot | None,
        pending: HistoryDownloadBatch,
        publication_id: str,
    ) -> HistoryBatchInput | HistoryMaintenanceStatus:
        state = self.dependencies.control.load_state()
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
        current_source, current_calendar, current_universe = BuildHistoryControlIdentities(context)
        if pending.cutoff != calendar.open_dates[-1] or current_calendar.open_dates[-1] < pending.cutoff:
            raise RuntimeError("history_resume_cutoff_changed")
        advancing = current_calendar.open_dates[-1] > pending.cutoff
        if (
            tuple(day for day in calendar.open_dates if day >= current_calendar.open_dates[0])
            != tuple(day for day in current_calendar.open_dates if day <= pending.cutoff)
            or (not advancing and calendar.open_dates != current_calendar.open_dates)
        ):
            raise RuntimeError("history_resume_calendar_changed")
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
        allowed_codes = frozenset(item.code for item in context.universe)
        if self.repair_only and (
            not allowed_codes.issubset(previous_by_code)
            or any(
                not task.tencent_done and task.dates for task in self.dependencies.tasks.tasks(codes=allowed_codes)
            )
        ):
            return _status("blocked", "history_download_incomplete", self.configuration, current_snapshot)
        reference = self.dependencies.maintenance.read_reference(source.supplier_contract)
        if (
            advancing
            or source.supplier_contract.rsplit("_", 1)[-1] != current_source.supplier_contract.rsplit("_", 1)[-1]
            or universe.securities != current_universe.securities
        ):
            # Bind the current reference while retaining all durable task evidence.
            # Persist immutable parents before atomically switching the unpublished batch.
            reference = HistoryReferenceSnapshot(
                tuple(item.code for item in context.universe), context.industry_intervals, context.st_evidence
            )
            self.dependencies.control.save_inputs((current_source, current_calendar, current_universe), reference, publication_id)
            current = HistoryDownloadBatch(
                pending.sequence,
                current_source.content_hash,
                current_calendar.content_hash,
                current_universe.content_hash,
                current_calendar.open_dates[-1],
            )
            if advancing:
                self.dependencies.tasks.rebind_context(
                    pending, current, tasks=self._advance_tasks(context, pending, frozenset(previous_by_code)),
                )
            elif previous_by_code.keys() != {item.code for item in current_universe.securities}:
                self.dependencies.tasks.rebind_context(
                    pending,
                    current,
                    tasks=tuple(
                        HistoryDownloadTask(item.code, context.calendar.expected_dates(item))
                        for item in context.universe
                    ),
                    reopened_codes=frozenset(item.code for item in context.universe) - previous_by_code.keys(),
                )
            else:
                self.dependencies.tasks.rebind_context(pending, current)
            return HistoryBatchInput(context, current_source, current_calendar, current_universe, reference, current, False)
        return HistoryBatchInput(context, source, calendar, universe, reference, pending, False)

    def _advance_tasks(
        self, context: HistorySupplierContext, pending: HistoryDownloadBatch, previous_codes: frozenset[str]
    ) -> tuple[HistoryDownloadTask, ...]:
        previous = {
            task.code: task for task in self.dependencies.tasks.tasks(codes=frozenset(item.code for item in context.universe))
        }
        retryable = self.dependencies.tasks.gaps_by_code(context.calendar.open_dates)
        tasks = []
        for security in context.universe:
            expected = context.calendar.expected_dates(security)
            task = previous.get(security.code)
            if task is None or security.code not in previous_codes:
                tasks.append(HistoryDownloadTask(security.code, expected))
                continue
            new = {day for day in expected if day > pending.cutoff}
            missing = set(retryable.get(security.code, ()))
            remaining = set(task.dates) if not task.tencent_done else set()
            anchors = (
                tuple(day for day in expected if day <= pending.cutoff)[-self.configuration.reread_sessions:]
                if new or missing else ()
            )
            required = remaining | missing | new | set(anchors)
            dates = tuple(day for day in expected if day in required)
            tasks.append(replace(task, dates=dates, tencent_done=task.tencent_done and not new))
        return tuple(tasks)

    def _repair_reference(
        self, source: HistorySourceIdentity, reference: HistoryReferenceSnapshot, publication_id: str
    ) -> None:
        try:
            self.dependencies.maintenance.read_reference(source.supplier_contract)
        except (OSError, ValueError):
            self.dependencies.control.save_reference(reference, publication_id)

    def _download(self, inputs: HistoryBatchInput, current_snapshot: HistorySnapshot | None) -> HistorySnapshot:
        with self.dependencies.prices.open_batch(
            inputs, current_snapshot,
        ) as prices:
            allowed_codes = frozenset(item.code for item in inputs.context.universe)
            PublishHistoryProgress(self.progress, "initializing", "started", current_item="replaying_repairs")
            prices.replay_repairs(allowed_codes)
            PublishHistoryProgress(self.progress, "initializing", "completed", (1, 1), "replaying_repairs")
            if not self.repair_only:
                prices.download_tencent()
            prices.prepare_retry_pass()
            gaps = self.dependencies.tasks.gaps_by_code(inputs.calendar.open_dates, retryable_only=False)
            batch_id = self.dependencies.maintenance.new_repair_identity()
            selected = tuple(item for item in inputs.context.universe if item.code in gaps)
            attempted = repaired = 0
            PublishHistoryProgress(self.progress, "baostock_repair", "started", (0, len(selected)))
            for completed, security in enumerate(selected, 1):
                if self.cancel():
                    raise KeyboardInterrupt
                dates = inputs.context.calendar.expected_dates(security)
                eligible = tuple(day for day in gaps[security.code] if day in dates)
                if not prices.reserve_repair(security.code, eligible, batch_id, explicit=self.repair_only):
                    continue
                attempted += 1
                try:
                    candidate = None
                    if prices.repair_source(security.code, inputs.batch.sequence) == "baostock":
                        previous = PublishedHistoryWindow(
                            security.code,
                            tuple(ProjectHistoryCell(cell) for cell in prices.read_prices(security.code, dates)),
                        )
                        obtained = {cell.trade_date for cell in previous.cells if cell.status in ("complete", "supplier_marked_suspended")}
                        missing = tuple(day for day in dates if day not in obtained)
                        tail_dates = dates[-max(5, len(missing) + 3):]
                        if len(missing) <= 5 and set(missing).issubset(tail_dates):
                            tail = self.supplier.fetch_baostock_window(security, tail_dates)
                            candidate = MergeDailyTail(previous, tail, dates)
                    if candidate is None:
                        candidate = self.supplier.fetch_baostock_window(security, dates)
                    prices.replace_repair(security, dates, candidate, inputs.batch.sequence)
                except (OSError, RuntimeError, ValueError) as exc:
                    reason = self.dependencies.maintenance.download_failure(exc)
                    prices.fail_repair(security.code, batch_id, reason)
                    if self.progress is not None:
                        self.progress.publish(HistorySyncProgress(
                            "baostock_repair", "failed", completed, len(selected), security.code,
                            failure_reason=reason,
                        ))
                else:
                    repaired += 1
                    PublishHistoryProgress(self.progress, "baostock_repair", "completed", (completed, len(selected)), security.code)
            prices.replay_repairs(allowed_codes)
            if self.progress is not None:
                self.progress.publish(HistorySyncProgress(
                    "baostock_repair", "completed", len(selected), len(selected),
                    repair_summary=HistoryRepairSummary(attempted, repaired, len(selected) - attempted),
                ))
            PublishHistoryProgress(self.progress, "preparing_publication", "started")
            with prices.publication_activity():
                remaining_gaps = prices.remaining_gaps()
                for security in inputs.context.universe:
                    if self.cancel():
                        raise KeyboardInterrupt
                    if missing_dates := remaining_gaps.get(security.code):
                        prices.record_missing(security, missing_dates)
                if self.price_review_budget is not None and not self.repair_only and not inputs.reviewed:
                    prices.review_inferred(self.price_review_budget)
                if self.cancel():
                    raise KeyboardInterrupt
            PublishHistoryProgress(self.progress, "preparing_publication", "completed", (1, 1))
            partitions = prices.publishable_partitions(inputs.calendar.open_dates)
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
        PublishHistoryProgress(self.progress, "publishing_snapshot", "started")
        self.dependencies.maintenance.publish(
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
        PublishHistoryProgress(self.progress, "publishing_snapshot", "completed", (1, 1))
        return snapshot


def _validate_context(context: HistorySupplierContext, sessions: int) -> str:
    if len(context.calendar.open_dates) != sessions:
        raise RuntimeError("supplier_calendar_incomplete")
    if context.reference_id is None:
        raise RuntimeError("history_reference_publication_missing")
    return context.reference_id


def _status(  # noqa: PLR0913 - immutable status projection keeps the public fields explicit
    state: HistoryMaintenanceState,
    reason: str | None,
    configuration: HistorySyncConfiguration,
    snapshot: HistorySnapshot | None,
    *,
    unresolved_price_cells: int = 0,
    price_gap_summary: HistoryPriceGapSummary | None = None,
    price_review: HistoryPriceReviewSummary | None = None,
    industry_readiness: HistoryIndustryReadiness | None = None,
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
        price_review=price_review,
        industry_readiness=industry_readiness,
    )


__all__ = ["DownloadHistoryUseCase"]
