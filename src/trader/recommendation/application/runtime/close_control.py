from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from trader.recommendation.application.ports.runtime import (
    CycleRequest,
    DataRefreshPendingError,
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
    ShanghaiNow,
)
from trader.recommendation.application.runtime.schedule_requests import (
    FailureCode,
)
from trader.recommendation.application.runtime.trading_session import SessionGuard, StaleSessionError
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
    """Own close-input recovery and the bounded freeze/settlement reservations."""

    def __init__(self, dependencies: RuntimeDependencies, hooks: CloseControlHooks) -> None:
        self._dependencies = dependencies
        self._hooks = hooks
        self._lock = threading.RLock()
        self._control = dependencies.control_pool
        self._control_pending: set[str] = set()
        self._control_completed: OrderedDict[str, None] = OrderedDict()
        self._close_input: RefreshOutcome | None = None

    def rotate_session(self) -> None:
        with self._lock:
            self._close_input = None

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
                    PipelineTaskRequest(
                        PipelineTask.CLOSE_QUOTES, ShanghaiNow(self._dependencies.clock.now()), (), scheduled.session
                    )
                )
                if outcome.completed_at.date() != at.date():
                    raise DataRefreshUnavailableError("close_input_trade_date_mismatch")
            except DataRefreshPendingError:
                self._hooks.pipeline_result(scheduled, SchedulePointResult.RETRY)
                return
            except DataRefreshUnavailableError as exc:
                self._hooks.failure("refresh", FailureCode(exc, "refresh_unavailable"), None)
                self._hooks.pipeline_result(scheduled, SchedulePointResult.RETRY)
                return
            with SessionGuard(scheduled.session), self._lock:
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
                self._hooks.submit_cycle(
                    replace(self._hooks.scheduled_request(strategy, at, "close_fallback"), session=scheduled.session)
                )

    def freeze_close_fallback(
        self,
        request: CycleRequest,
        current: ScoredDecision,
        *,
        recovery_path: Literal["current", "close_rebuild"],
    ) -> None:
        native_version = dict(current.input_versions).get("native", current.version)
        try:
            with SessionGuard(request.session):
                self._dependencies.freezes.freeze_close_fallback(
                    request.strategy,
                    request.observed_at,
                    current,
                    recovery_path=recovery_path,
                    official_close_version=f"official-close:{native_version}",
                )
        except StaleSessionError:
            return
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
        generation = scheduled.session.generation if scheduled.session is not None else 0
        key = f"freeze:{at.date().isoformat()}:{strategy.value}:{generation}"
        if not self._reserve_control(key):
            return False
        future = self._control.submit_urgent(self._run_freeze, key, strategy, at, scheduled)
        if future is None:
            self._finish_control(key, success=False)
            self._hooks.failure("freeze", "freeze_capacity_rejected", strategy)
            return False
        return True

    def _run_freeze(
        self,
        key: str,
        strategy: Strategy,
        at: datetime,
        scheduled: ScheduledPipelineTask,
    ) -> None:
        success = False
        missed = False
        try:
            with SessionGuard(scheduled.session):
                current = self._dependencies.index.snapshot(strategy).current
                if (
                    scheduled.session is not None
                    and scheduled.session.generation > 1
                    and (current is None or not scheduled.session.allows_version(current.version))
                ):
                    # No eligible session input: release the normal freeze point so
                    # close recovery can build a fresh, explicitly labelled fallback.
                    missed = True
                    return
                self._dependencies.freezes.freeze(strategy, at, current)
        except StaleSessionError:
            return
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
            try:
                with SessionGuard(scheduled.session):
                    self._dependencies.cadence.record_point_result(
                        at.date().isoformat(),
                        scheduled.schedule_point or SchedulePoint.AFTERNOON_FREEZE,
                        strategy.value,
                        SchedulePointResult.MISSED
                        if missed
                        else (SchedulePointResult.COMPLETED if success else SchedulePointResult.RETRY),
                        at=ShanghaiNow(self._dependencies.clock.now()),
                    )
            except StaleSessionError:
                pass

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
                        at=ShanghaiNow(self._dependencies.clock.now()),
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
