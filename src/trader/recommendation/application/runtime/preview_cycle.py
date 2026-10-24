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
from trader.recommendation.application.runtime.cadence import PipelineTask
from trader.recommendation.application.runtime.runtime_dependencies import RuntimeDependencies
from trader.recommendation.application.runtime.schedule import ShanghaiNow
from trader.recommendation.application.runtime.schedule_requests import FailureCode
from trader.recommendation.application.runtime.trading_session import SessionLease
from trader.recommendation.domain.publication.decision_identity import ScoredDecision
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
        execute_pair: Callable[[tuple[CycleRequest, CycleRequest]], None],
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

    def execute(self, context: PreviewContext, session: SessionLease | None) -> None:
        with self._lock:
            if self._status.state != "waiting_inputs":
                return
            self._status = PreviewCycleStatus("running")
        try:
            self._dependencies.data.prepare_preview(context)
            self._refresh(PipelineTask.FULL_MARKET, context, session)
        except DataRefreshPendingError as exc:
            self._set_status("waiting_inputs", FailureCode(exc, "preview_inputs_pending"))
            return
        except (DataRefreshUnavailableError, OSError, RuntimeError, ValueError) as exc:
            self._set_status("failed", FailureCode(exc, "preview_inputs_unavailable"))
            return
        try:
            self._refresh(PipelineTask.INDUSTRY_HEAT, context, session)
            self._refresh(PipelineTask.CANDIDATE_QUOTES, context, session)
            self._refresh(PipelineTask.INTRADAY_TAIL, context, session)
            observed_at = ShanghaiNow(self._dependencies.clock.now())
            requests = tuple(
                replace(
                    self._build_request(strategy, observed_at, "before_close"),
                    preview_context=context,
                    session=session,
                )
                for strategy in (Strategy.TOMORROW, Strategy.D25)
            )
            self._execute_pair((requests[0], requests[1]))
        except DataRefreshPendingError as exc:
            self._set_status("waiting_inputs", FailureCode(exc, "preview_inputs_pending"))
            return
        except (DataRefreshUnavailableError, OSError, RuntimeError, ValueError) as exc:
            self._set_status("failed", FailureCode(exc, "preview_run_failed"))
            return
        decisions = tuple(
            self._dependencies.index.snapshot(strategy).current for strategy in (Strategy.TOMORROW, Strategy.D25)
        )
        if all(isinstance(decision, ScoredDecision) and decision.preview_context == context for decision in decisions):
            self._set_status("ready")
        else:
            self._set_status("failed", "preview_decision_unavailable")

    def _refresh(self, task: PipelineTask, context: PreviewContext, session: SessionLease | None) -> None:
        started = self._dependencies.monotonic()
        try:
            self._dependencies.data.refresh_task(
                PipelineTaskRequest(
                    task,
                    ShanghaiNow(self._dependencies.clock.now()),
                    session=session,
                    preview_context=context,
                )
            )
        finally:
            self._dependencies.latency.record_duration(
                f"preview:{task.value}", (self._dependencies.monotonic() - started) * 1000.0
            )

    def _set_status(
        self, state: Literal["waiting_inputs", "running", "ready", "failed"], error: str | None = None
    ) -> None:
        with self._lock:
            self._status = PreviewCycleStatus(state, error)
