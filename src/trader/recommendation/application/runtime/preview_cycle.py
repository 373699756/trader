"""One complete non-trading-day computation, published only to memory."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from trader.recommendation.application.ports.runtime import (
    CycleRequest,
    DataRefreshPendingError,
    DataRefreshUnavailableError,
    PipelineTaskRequest,
)
from trader.recommendation.application.runtime.cadence import PipelineTask, ScheduledPipelineTask
from trader.recommendation.application.runtime.runtime_dependencies import RuntimeDependencies
from trader.recommendation.application.runtime.schedule import ShanghaiNow
from trader.recommendation.application.runtime.schedule_requests import FailureCode
from trader.recommendation.application.runtime.trading_session import SessionGuard
from trader.recommendation.domain.publication.freeze_identity import ScoredFreeze
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.publication.preview import PreviewContext


@dataclass(frozen=True)
class PreviewCycleStatus:
    state: Literal["waiting_inputs", "running", "ready", "failed"] = "waiting_inputs"
    error: str | None = None


class PreviewCycleRunner:
    def __init__(
        self,
        dependencies: RuntimeDependencies,
        execute_pair: Callable[[tuple[CycleRequest, CycleRequest], int], None],
        build_request: Callable[[Strategy, datetime, str], CycleRequest],
    ) -> None:
        self._dependencies = dependencies
        self._execute_pair = execute_pair
        self._build_request = build_request
        self._lock = threading.Lock()
        self._status = PreviewCycleStatus()

    def status(self) -> PreviewCycleStatus:
        with self._lock:
            return self._status

    def execute(self, context: PreviewContext, scheduled: ScheduledPipelineTask) -> None:
        sequence = scheduled.round_sequence
        if sequence is None or scheduled.round_deadline is None or scheduled.round_deadline_monotonic is None:
            raise ValueError("preview requires an admitted recommendation round")
        with self._lock:
            if self._status.state != "waiting_inputs":
                return
            self._status = PreviewCycleStatus("running")
        try:
            self._check_round(scheduled)
            self._dependencies.data.prepare_preview(context)
            self._refresh(PipelineTask.FULL_MARKET, context, scheduled)
        except DataRefreshPendingError as exc:
            self._defer(sequence, exc)
            return
        except (DataRefreshUnavailableError, OSError, RuntimeError, ValueError) as exc:
            self._set_status("failed", FailureCode(exc, "preview_inputs_unavailable"))
            return
        try:
            if not self._dependencies.rounds.prepared(self._dependencies.round_profile, sequence):
                self._check_round(scheduled)
                raise DataRefreshUnavailableError("preview_round_cancelled")
            self._refresh(PipelineTask.INDUSTRY_HEAT, context, scheduled)
            self._refresh(PipelineTask.CANDIDATE_QUOTES, context, scheduled)
            self._refresh(PipelineTask.INTRADAY_TAIL, context, scheduled)
            if not self._dependencies.rounds.collected(self._dependencies.round_profile, sequence):
                self._check_round(scheduled)
                raise DataRefreshUnavailableError("preview_round_cancelled")
            observed_at = ShanghaiNow(self._dependencies.clock.now())
            requests = tuple(
                replace(
                    self._build_request(strategy, observed_at, "before_close"),
                    preview_context=context,
                    session=scheduled.session,
                    round_started_monotonic=scheduled.round_started_monotonic,
                    round_deadline_monotonic=scheduled.round_deadline_monotonic,
                )
                for strategy in (Strategy.TOMORROW, Strategy.D25)
            )
            self._execute_pair((requests[0], requests[1]), sequence)
        except DataRefreshPendingError as exc:
            self._defer(sequence, exc)
            return
        except (DataRefreshUnavailableError, OSError, RuntimeError, ValueError) as exc:
            self._set_status("failed", FailureCode(exc, "preview_run_failed"))
            return
        freezes = tuple(
            self._dependencies.index.snapshot(strategy).current for strategy in (Strategy.TOMORROW, Strategy.D25)
        )
        if all(isinstance(freeze, ScoredFreeze) and freeze.preview_context == context for freeze in freezes):
            self._set_status("ready")
        elif self._dependencies.rounds.inputs_pending(sequence):
            self._set_status("waiting_inputs", "preview_inputs_pending")
        else:
            self._set_status("failed", "preview_freeze_unavailable")

    def _check_round(self, scheduled: ScheduledPipelineTask) -> None:
        if scheduled.round_sequence is None or self._dependencies.rounds.cancelled(scheduled.round_sequence):
            if scheduled.round_sequence is not None and self._dependencies.rounds.inputs_pending(scheduled.round_sequence):
                raise DataRefreshPendingError("preview_inputs_pending")
            raise DataRefreshUnavailableError("preview_round_cancelled")
        with SessionGuard(scheduled.session):
            pass

    def _defer(self, sequence: int, failure: DataRefreshPendingError) -> None:
        state: Literal["waiting_inputs", "failed"] = (
            "waiting_inputs" if self._dependencies.rounds.defer(sequence) else "failed"
        )
        self._set_status(state, FailureCode(failure, "preview_inputs_pending"))

    def _refresh(self, task: PipelineTask, context: PreviewContext, scheduled: ScheduledPipelineTask) -> None:
        self._check_round(scheduled)
        started = self._dependencies.monotonic()
        try:
            self._dependencies.data.refresh_task(
                PipelineTaskRequest(
                    task,
                    ShanghaiNow(self._dependencies.clock.now()),
                    session=scheduled.session,
                    preview_context=context,
                    round_deadline=scheduled.round_deadline,
                    round_inputs=self._dependencies.rounds.inputs(
                        self._dependencies.round_profile, scheduled.round_sequence,
                    ),
                )
            )
            self._check_round(scheduled)
        finally:
            self._dependencies.latency.record_duration(
                f"preview:{task.value}", (self._dependencies.monotonic() - started) * 1000.0
            )

    def _set_status(
        self, state: Literal["waiting_inputs", "running", "ready", "failed"], error: str | None = None
    ) -> None:
        with self._lock:
            self._status = PreviewCycleStatus(state, error)
