from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from trader.recommendation.application.ports.runtime import (
    CycleRequest,
    DataRefreshUnavailableError,
    FreezeUnavailableError,
    PipelineTaskRequest,
    RefreshOutcome,
    SettlementUnavailableError,
)
from trader.recommendation.application.runtime.cadence import (
    PipelineTask,
    ScheduledPipelineTask,
    SchedulePointResult,
)
from trader.recommendation.application.runtime.latest_wins import (
    LatestWinsOffer,
    LatestWinsStatus,
)
from trader.recommendation.application.runtime.runtime_dependencies import RuntimeDependencies
from trader.recommendation.application.runtime.schedule import (
    SchedulePoint,
    shanghai_now,
)
from trader.recommendation.application.runtime.schedule_requests import (
    failure_code,
)
from trader.recommendation.domain.publication.decision_identity import (
    ScoredDecision,
)
from trader.recommendation.domain.publication.models import Strategy


@dataclass(frozen=True)
class CloseControlHooks:
    failure: Callable[[str, str, Strategy | None], None]
    success: Callable[[str, Strategy | None], None]
    pipeline_result: Callable[[ScheduledPipelineTask, SchedulePointResult], None]
    submit_cycle: Callable[[CycleRequest], LatestWinsOffer]
    scheduled_request: Callable[[Strategy, datetime, str], CycleRequest]
    lane_status: Callable[[Strategy], LatestWinsStatus]


class CloseControlCoordinator:
    """Own close-input recovery and the bounded checkpoint/freeze/settlement reservations."""

    def __init__(self, dependencies: RuntimeDependencies, hooks: CloseControlHooks) -> None:
        self._dependencies = dependencies
        self._hooks = hooks
        self._lock = threading.RLock()
        self._control = dependencies.control_pool
        self._control_pending: set[str] = set()
        self._control_completed: OrderedDict[str, None] = OrderedDict()
        self._close_input: RefreshOutcome | None = None

    def process_close_recovery(self, scheduled: ScheduledPipelineTask) -> None:
        at = scheduled.scheduled_at
        missing = tuple(
            strategy
            for strategy in (Strategy.TOMORROW, Strategy.D25)
            if (formal := self._dependencies.index.snapshot(strategy).formal) is None or formal.trade_date != at.date()
        )
        with self._lock:
            close_input = self._close_input
        if missing and (close_input is None or close_input.completed_at.date() != at.date()):
            try:
                outcome = self._dependencies.data.refresh_task(
                    PipelineTaskRequest(PipelineTask.CLOSE_QUOTES, shanghai_now(self._dependencies.clock.now()), ())
                )
                if outcome.completed_at.date() != at.date():
                    raise DataRefreshUnavailableError("close_input_trade_date_mismatch")
            except DataRefreshUnavailableError as exc:
                self._hooks.failure("refresh", failure_code(exc, "refresh_unavailable"), None)
                self._hooks.pipeline_result(scheduled, SchedulePointResult.RETRY)
                return
            with self._lock:
                self._close_input = outcome
        # Reconcile authoritative formal/control state on later ticks; handoff is not completion.
        with self._lock:
            completed = f"settlement:{at.date().isoformat()}" in self._control_completed
            self._hooks.pipeline_result(
                scheduled, SchedulePointResult.COMPLETED if completed else SchedulePointResult.RETRY
            )
        if completed:
            return
        if not missing:
            self.submit_settlement(at)
            return
        for strategy in missing:
            lane = self._hooks.lane_status(strategy)
            if not lane.running and not lane.pending:
                self._hooks.submit_cycle(self._hooks.scheduled_request(strategy, at, "close_fallback"))

    def freeze_close_fallback(
        self,
        request: CycleRequest,
        current: ScoredDecision,
        *,
        recovery_path: Literal["current", "close_rebuild"],
    ) -> None:
        native_version = dict(current.input_versions).get("native", current.version)
        try:
            self._dependencies.freezes.freeze_close_fallback(
                request.strategy,
                request.observed_at,
                current,
                recovery_path=recovery_path,
                official_close_version=f"official-close:{native_version}",
            )
        except FreezeUnavailableError:
            self._hooks.failure("freeze", "close_fallback_unavailable", request.strategy)
        except Exception as exc:
            self._hooks.failure("freeze", f"close_fallback_unexpected:{type(exc).__name__}", request.strategy)
        else:
            with self._lock:
                self._hooks.success("freeze", request.strategy)

    def submit_freeze(
        self,
        strategy: Strategy,
        at: datetime,
        *,
        scheduled: ScheduledPipelineTask,
    ) -> bool:
        key = f"freeze:{at.date().isoformat()}:{strategy.value}"
        if not self._reserve_control(key):
            return False
        future = self._control.submit_urgent(self._run_freeze, key, strategy, at, scheduled)
        if future is None:
            self._finish_control(key, success=False)
            self._hooks.failure("freeze", "freeze_capacity_rejected", strategy)
            return False
        return True

    def submit_checkpoint(
        self,
        strategy: Strategy,
        at: datetime,
        *,
        scheduled: ScheduledPipelineTask,
    ) -> bool:
        key = f"checkpoint:{at.date().isoformat()}:{strategy.value}"
        if not self._reserve_control(key):
            return False
        future = self._control.submit_urgent(self._run_checkpoint, key, strategy, at, scheduled)
        if future is None:
            self._finish_control(key, success=False)
            self._hooks.failure("checkpoint", "checkpoint_capacity_rejected", strategy)
            return False
        return True

    def _run_checkpoint(
        self,
        key: str,
        strategy: Strategy,
        at: datetime,
        scheduled: ScheduledPipelineTask,
    ) -> None:
        success = False
        try:
            self._dependencies.freezes.capture_checkpoint(strategy, at)
        except FreezeUnavailableError as exc:
            self._hooks.failure("checkpoint", failure_code(exc, "checkpoint_unavailable"), strategy)
        except Exception as exc:
            self._hooks.failure("checkpoint", f"checkpoint_unexpected:{type(exc).__name__}", strategy)
        else:
            success = True
            with self._lock:
                self._hooks.success("checkpoint", strategy)
        finally:
            self._finish_control(key, success=success)
            self._dependencies.cadence.record_point_result(
                at.date().isoformat(),
                scheduled.schedule_point or SchedulePoint.AFTERNOON_CHECKPOINT,
                strategy.value,
                SchedulePointResult.COMPLETED if success else SchedulePointResult.RETRY,
                at=shanghai_now(self._dependencies.clock.now()),
            )

    def _run_freeze(
        self,
        key: str,
        strategy: Strategy,
        at: datetime,
        scheduled: ScheduledPipelineTask,
    ) -> None:
        success = False
        try:
            current = self._dependencies.index.snapshot(strategy).current
            self._dependencies.freezes.freeze(strategy, at, current)
        except FreezeUnavailableError:
            self._hooks.failure("freeze", "freeze_unavailable", strategy)
        except Exception as exc:
            self._hooks.failure("freeze", f"freeze_unexpected:{type(exc).__name__}", strategy)
        else:
            success = True
            with self._lock:
                self._hooks.success("freeze", strategy)
        finally:
            self._finish_control(key, success=success)
            self._dependencies.cadence.record_point_result(
                at.date().isoformat(),
                scheduled.schedule_point or SchedulePoint.AFTERNOON_FREEZE,
                strategy.value,
                SchedulePointResult.COMPLETED if success else SchedulePointResult.RETRY,
                at=shanghai_now(self._dependencies.clock.now()),
            )

    def submit_settlement(self, at: datetime) -> None:
        key = f"settlement:{at.date().isoformat()}"
        if not self._reserve_control(key):
            return
        future = self._control.submit(self._run_settlement, key, at)
        if future is None:
            self._finish_control(key, success=False)
            self._hooks.failure("settlement", "settlement_capacity_rejected", None)

    def _run_settlement(self, key: str, at: datetime) -> None:
        success = False
        try:
            self._dependencies.settlement.settle(at)
        except SettlementUnavailableError:
            self._hooks.failure("settlement", "settlement_unavailable", None)
        except Exception as exc:
            self._hooks.failure("settlement", f"settlement_unexpected:{type(exc).__name__}", None)
        else:
            success = True
            with self._lock:
                self._hooks.success("settlement", None)
        finally:
            with self._lock:
                self._finish_control(key, success=success)
                if success:
                    self._dependencies.cadence.record_point_result(
                        at.date().isoformat(),
                        SchedulePoint.CLOSE_QUOTES,
                        "-",
                        SchedulePointResult.COMPLETED,
                        at=shanghai_now(self._dependencies.clock.now()),
                    )

    def _reserve_control(self, key: str) -> bool:
        with self._lock:
            if key in self._control_pending or key in self._control_completed:
                return False
            self._control_pending.add(key)
            return True

    def _finish_control(self, key: str, *, success: bool) -> None:
        with self._lock:
            self._control_pending.discard(key)
            if not success:
                return
            self._control_completed[key] = None
            while len(self._control_completed) > 128:
                self._control_completed.popitem(last=False)
