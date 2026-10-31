from __future__ import annotations

import math
import threading
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from datetime import time as wall_time
from typing import Literal

from trader.infra.shutdown import ShutdownDeadline, ShutdownReport, ShutdownStep
from trader.recommendation.application.pipeline.freeze_publish.freeze_events import (
    FreezeCommitted,
    FreezeObservation,
)
from trader.recommendation.application.pipeline.freeze_publish.publication_io import (
    ObservePublicationIo,
    PublicationIoTarget,
)
from trader.recommendation.application.ports.long import LongRefreshRequest
from trader.recommendation.application.ports.runtime import (
    CycleRequest,
    DataRefreshPendingError,
    DataRefreshUnavailableError,
    PipelineTaskRequest,
    TradingCalendarUnavailableError,
)
from trader.recommendation.application.runtime.cadence import (
    PERIODIC_TASKS,
    PipelineTask,
    ScheduledPipelineTask,
    SchedulePointResult,
    TaskExecutionBudgetSeconds,
)
from trader.recommendation.application.runtime.close_control import CloseControlCoordinator, CloseControlHooks
from trader.recommendation.application.runtime.cycle_execution import (
    CycleExecutionHooks,
    HybridUpgradeRequest,
    RecommendationCycleExecutor,
)
from trader.recommendation.application.runtime.latest_wins import (
    LatestWinsOffer,
    LatestWinsTelemetry,
    LatestWinsWorker,
)
from trader.recommendation.application.runtime.preview_cycle import PreviewCycleRunner
from trader.recommendation.application.runtime.runtime_dependencies import RuntimeDependencies
from trader.recommendation.application.runtime.runtime_issues import RuntimeIssue, RuntimeIssueIndex
from trader.recommendation.application.runtime.schedule import (
    SHANGHAI,
    FreezeAt,
    MarketPhase,
    ScheduleFreeze,
    SchedulePoint,
    ShanghaiNow,
)
from trader.recommendation.application.runtime.schedule_requests import (
    CycleCorrelationId,
    CycleOrderKey,
    CyclePhase,
    FailureCode,
    PipelineTaskCorrelationId,
    PipelineTaskOrderKey,
)
from trader.recommendation.application.runtime.status import SchedulerRuntimeStatus, TradingCalendarRuntimeStatus
from trader.recommendation.application.runtime.trading_session import SessionGuard, StaleSessionError, TradingSession
from trader.recommendation.domain.evidence.pipeline import StageState
from trader.recommendation.domain.publication.freeze_identity import (
    FreezeIdentity,
    LongProjection,
    ScoredFreeze,
)
from trader.recommendation.domain.publication.models import Strategy

_DATA_TASKS = (
    PipelineTask.FULL_MARKET,
    PipelineTask.CANDIDATE_QUOTES,
    PipelineTask.TOPK_QUOTES,
    PipelineTask.INTRADAY_TAIL,
    PipelineTask.INDUSTRY_HEAT,
    PipelineTask.CLOSE_QUOTES,
)
_DATA_LANES = tuple(task for task in _DATA_TASKS if task not in {PipelineTask.CLOSE_QUOTES})
_SCORING_INPUT_TASKS = frozenset(
    {
        PipelineTask.CANDIDATE_QUOTES,
        PipelineTask.INTRADAY_TAIL,
    }
)
_CALENDAR_RETRY_DELAYS_SECONDS = (30.0, 60.0, 120.0, 300.0)


class SchedulerRuntime:
    """Scheduler-facing runtime that owns no legacy Pipeline resources."""

    def __init__(
        self,
        dependencies: RuntimeDependencies,
        *,
        config_version: str,
        shutdown_timeout_seconds: float,
    ) -> None:
        if not config_version:
            raise ValueError("scheduler runtime config version must not be empty")
        if not math.isfinite(shutdown_timeout_seconds) or shutdown_timeout_seconds <= 0.0:
            raise ValueError("scheduler shutdown timeout must be finite and positive")
        contract = dependencies.reviews.runtime_contract
        if contract.daily_physical_limit != 168 or not contract.shared_cache or not contract.shared_single_flight:
            raise ValueError("scheduler runtime requires the shared 168-request DeepSeek boundary")
        self._dependencies = dependencies
        dependencies.rounds.register(dependencies.round_profile)
        self._research = dependencies.research_factory()
        self._config_version = config_version
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._lock = threading.RLock()
        self._session = TradingSession(dependencies.clock.now, dependencies.monotonic)
        self._planned_generation = 0
        self._preview_long_submitted = False
        self._running = False
        self._stopped = False
        self._shutdown_report: ShutdownReport | None = None
        self._phase = MarketPhase.CLOSED
        self._sequences = dict.fromkeys(Strategy, 0)
        self._control = dependencies.control_pool
        self._lanes = {
            strategy: LatestWinsWorker(
                f"trader-{strategy.value}",
                lambda request: self._cycles.execute(request),
                order_key=CycleOrderKey,
                telemetry=LatestWinsTelemetry(
                    dependencies.latency,
                    CycleCorrelationId,
                    lambda request: f"score:{request.strategy.value}",
                ),
            )
            for strategy in Strategy
        }
        self._hybrid_lanes: dict[Strategy, LatestWinsWorker[HybridUpgradeRequest]] = {
            strategy: LatestWinsWorker(
                f"trader-hybrid-{strategy.value}",
                lambda request: self._cycles.upgrade(request),
                order_key=_hybrid_order_key,
                telemetry=LatestWinsTelemetry(
                    dependencies.latency,
                    lambda request: f"hybrid:{CycleCorrelationId(request.cycle)}",
                    lambda request: f"hybrid:{request.cycle.strategy.value}",
                ),
            )
            for strategy in (Strategy.TOMORROW, Strategy.D25)
        }
        self._task_lanes: dict[PipelineTask, LatestWinsWorker[ScheduledPipelineTask]] = {
            task: LatestWinsWorker(
                f"trader-task-{task.value}",
                self._process_pipeline_task,
                order_key=PipelineTaskOrderKey,
                telemetry=LatestWinsTelemetry(
                    dependencies.latency,
                    PipelineTaskCorrelationId,
                    lambda request: f"data:{request.task.value}",
                ),
            )
            for task in (PipelineTask.CLOSE_QUOTES,)
        }
        self._score_lane = LatestWinsWorker(
            "trader-task-score-wave",
            self._process_score_wave,
            order_key=PipelineTaskOrderKey,
            on_discard=self._discard_score_wave,
            telemetry=LatestWinsTelemetry(
                dependencies.latency,
                PipelineTaskCorrelationId,
                lambda request: f"score-wave:{request.task.value}",
            ),
        )
        self._refresh_failure_count = 0
        self._freeze_failure_count = 0
        self._review_failure_count = 0
        self._overlay_publish_count = 0
        self._overlay_failure_count = 0
        self._local_publish_count = 0
        self._hybrid_publish_count = 0
        self._publish_rejection_count = 0
        self._observer_rejection_count = 0
        self._freeze_completed_count = 0
        self._seal_failure_count = 0
        self._settlement_completed_count = 0
        self._settlement_failure_count = 0
        self._issues = RuntimeIssueIndex()
        self._calendar_state: Literal["unknown", "ready", "unavailable"] = "unknown"
        self._calendar_trade_date: date | None = None
        self._calendar_is_trading_day: bool | None = None
        self._calendar_consecutive_failure_count = 0
        self._calendar_next_retry_at: datetime | None = None
        self._close_control = CloseControlCoordinator(
            dependencies,
            CloseControlHooks(
                self._record_failure,
                self._record_control_success,
                self._record_pipeline_result,
                self.submit_cycle,
                self._scheduled_request,
                lambda strategy: self._lanes[strategy].status(),
            ),
        )
        self._cycles = RecommendationCycleExecutor(
            dependencies,
            close_control=self._close_control,
            research=self._research,
            hooks=CycleExecutionHooks(
                lambda strategy: self._lanes[strategy],
                lambda strategy: self._hybrid_lanes[strategy],
                self._record_failure,
                lambda hybrid, event, identity: self._record_publish(hybrid=hybrid, event=event, identity=identity),
                lambda event, identity: self._record_publish(
                    hybrid=False, event=event, identity=identity, publish_event=False
                ),
                self._record_publish_rejection,
            ),
        )
        self._preview = PreviewCycleRunner(
            dependencies,
            lambda requests, sequence: self._cycles.execute_round_pair(requests, round_sequence=sequence),
            self._scheduled_request,
        )

    def start(self) -> bool:
        with self._lock:
            if self._running:
                return False
            if self._stopped:
                raise RuntimeError("scheduler runtime cannot restart after stop")
            self._running = True
        try:
            self._start_components()
        except BaseException:
            self._abort_start()
            raise
        return True

    def _start_components(self) -> None:
        if not self._dependencies.observer.start():
            raise RuntimeError("freeze observer did not start")
        if not self._control.start():
            raise RuntimeError("control executor did not start")
        self._start_task_lanes()
        for strategy in Strategy:
            if not self._lanes[strategy].start():
                raise RuntimeError(f"{strategy.value} lane did not start")
        for strategy, lane in self._hybrid_lanes.items():
            if not lane.start():
                raise RuntimeError(f"{strategy.value} hybrid lane did not start")
        if not self._research.start():
            raise RuntimeError("research runtime did not start")

    def _start_task_lanes(self) -> None:
        for task, lane in self._task_lanes.items():
            if not lane.start():
                raise RuntimeError(f"{task.value} task lane did not start")
        if not self._score_lane.start():
            raise RuntimeError("score wave lane did not start")

    def _abort_start(self) -> None:
        deadline = ShutdownDeadline.start(self._shutdown_timeout_seconds)
        self._dependencies.rounds.withdraw(self._dependencies.round_profile)
        self._research.stop(wait=False, deadline=deadline)
        for task_lane in self._task_lanes.values():
            task_lane.close()
        self._score_lane.close()
        for strategy_lane in self._lanes.values():
            strategy_lane.close()
        for hybrid_lane in self._hybrid_lanes.values():
            hybrid_lane.close()
        for task_lane in self._task_lanes.values():
            task_lane.stop(deadline=deadline)
        self._score_lane.stop(deadline=deadline)
        for strategy_lane in self._lanes.values():
            strategy_lane.stop(deadline=deadline)
        for hybrid_lane in self._hybrid_lanes.values():
            hybrid_lane.stop(deadline=deadline)
        self._control.stop(wait=True, cancel_futures=True, deadline=deadline)
        self._dependencies.observer.stop(deadline=deadline)
        self._research.stop(wait=True, deadline=deadline)
        with self._lock:
            self._running = False
            self._stopped = True

    def submit_due(self, at: datetime | None = None) -> float:
        with self._lock:
            if not self._running:
                return 30.0
        observed_at = ShanghaiNow(at or self._dependencies.clock.now())
        scope = self._dependencies.run_scope
        if scope is not None:
            scope.initialize()
            if not scope.resolved:
                self._calendar_freeze(observed_at)
                return 30.0
            if scope.is_preview:
                return self._submit_preview(observed_at)
        session = self._session.capture()
        if session.generation != self._planned_generation:
            self._dependencies.cadence.rotate_session(observed_at, reason=self._session.status().discontinuity_reason)
            self._close_control.rotate_session()
            self._planned_generation = session.generation
        is_trading_day, retry_delay = self._calendar_freeze(observed_at)
        if is_trading_day is None:
            with self._lock:
                self._phase = MarketPhase.CLOSED
            return retry_delay
        batch = self._dependencies.cadence.plan(observed_at, is_trading_day=is_trading_day)
        with self._lock:
            self._phase = (
                batch.tasks[0].phase
                if batch.tasks
                else FreezeAt(
                    observed_at,
                    is_trading_day=is_trading_day,
                ).phase
            )
        round_tasks = tuple(task for task in batch.tasks if task.task in {*_DATA_LANES, PipelineTask.SCORE})
        for scheduled in batch.tasks:
            if scheduled in round_tasks:
                continue
            if not self._periodic_task_inflight(scheduled):
                self._dispatch_pipeline_task(replace(scheduled, session=session))
        self._submit_round_tasks(
            round_tasks, ScheduledPipelineTask(PipelineTask.SCORE, observed_at, self._phase, session=session),
        )
        return min(batch.next_delay_seconds, 1.0)

    def _submit_round_tasks(
        self, tasks: tuple[ScheduledPipelineTask, ...], scheduled: ScheduledPipelineTask,
    ) -> bool:
        rounds = self._dependencies.rounds
        profile = self._dependencies.round_profile
        admission = rounds.enter(profile, tasks)
        if admission is None:
            return False
        round_deadline = ShanghaiNow(self._dependencies.clock.now()) + timedelta(
            seconds=max(0.0, admission.deadline_monotonic - self._dependencies.monotonic())
        )
        if self._submit_scoring(replace(
            scheduled,
            round_sequence=admission.sequence,
            round_tasks=admission.tasks,
            round_started_monotonic=admission.started_monotonic,
            round_deadline_monotonic=admission.deadline_monotonic,
            round_deadline=round_deadline,
        )):
            return True
        rounds.finish(profile, admission.sequence)
        return False

    def _submit_preview(self, observed_at: datetime) -> float:
        observed_at = max(observed_at, ShanghaiNow(self._dependencies.clock.now()))
        scope = self._dependencies.run_scope
        long_quotes = self._dependencies.long_quotes
        with self._lock:
            if not self._preview_long_submitted and long_quotes is not None and scope is not None:
                self._preview_long_submitted = long_quotes.offer_refresh(
                    LongRefreshRequest(
                        observed_at,
                        "after_close",
                        deadline=observed_at
                        + timedelta(seconds=TaskExecutionBudgetSeconds(PipelineTask.LONG_QUOTES) or 3.0),
                        force=True,
                        session=self._session.capture(),
                        preview_context=scope.preview,
                    )
                )
        if self._preview.status().state != "waiting_inputs":
            return 30.0
        lane = self._score_lane.status()
        if not lane.running and not lane.pending:
            scheduled = ScheduledPipelineTask(
                PipelineTask.SCORE,
                observed_at,
                MarketPhase.BEFORE_CLOSE,
                session=self._session.capture(),
            )
            self._submit_round_tasks((scheduled,), scheduled)
        return 1.0

    def _calendar_freeze(self, observed_at: datetime) -> tuple[bool | None, float]:
        trade_date = observed_at.date()
        with self._lock:
            if self._calendar_trade_date != trade_date:
                self._calendar_state = "unknown"
                self._calendar_trade_date = trade_date
                self._calendar_is_trading_day = None
                self._calendar_consecutive_failure_count = 0
                self._calendar_next_retry_at = None
            next_retry_at = self._calendar_next_retry_at
            if self._calendar_state == "ready" and self._calendar_is_trading_day is not None:
                return self._calendar_is_trading_day, 0.0
            if next_retry_at is not None and observed_at < next_retry_at:
                return None, max(0.05, (next_retry_at - observed_at).total_seconds())
        try:
            is_trading_day = self._dependencies.calendar.is_trading_day(trade_date)
        except TradingCalendarUnavailableError:
            with self._lock:
                retry_index = min(
                    self._calendar_consecutive_failure_count,
                    len(_CALENDAR_RETRY_DELAYS_SECONDS) - 1,
                )
                retry_delay = _CALENDAR_RETRY_DELAYS_SECONDS[retry_index]
                self._calendar_state = "unavailable"
                self._calendar_is_trading_day = None
                self._calendar_consecutive_failure_count += 1
                self._calendar_next_retry_at = observed_at + timedelta(seconds=retry_delay)
                self._issues.record(
                    "calendar:calendar_unavailable",
                    "calendar",
                    None,
                    observed_at,
                )
            return None, retry_delay
        with self._lock:
            self._calendar_state = "ready"
            self._calendar_is_trading_day = is_trading_day
            self._calendar_consecutive_failure_count = 0
            self._calendar_next_retry_at = None
            self._issues.resolve(observed_at, stages=frozenset({"calendar"}))
        return is_trading_day, 0.0

    def _dispatch_pipeline_task(self, scheduled: ScheduledPipelineTask) -> None:
        if scheduled.session is None:
            scheduled = replace(scheduled, session=self._session.capture())
        self._dependencies.cadence.record_submission(scheduled, accepted=True, at=scheduled.scheduled_at)
        rejected_freezes: tuple[str, ...] = ()
        if scheduled.task in {*_DATA_LANES, PipelineTask.SCORE}:
            accepted = self._submit_round_tasks((scheduled,), scheduled)
        elif scheduled.task is PipelineTask.LONG_QUOTES:
            offer = self.submit_cycle(
                replace(
                    self._scheduled_request(Strategy.LONG, scheduled.scheduled_at, scheduled.phase.value),
                    session=scheduled.session,
                )
            )
            accepted = offer is not LatestWinsOffer.REJECTED
        elif scheduled.task is PipelineTask.FREEZE:
            submissions = {
                strategy: self._close_control.submit_freeze(
                    Strategy(strategy),
                    scheduled.scheduled_at,
                    scheduled=scheduled,
                )
                for strategy in scheduled.freeze_strategies
            }
            accepted = any(submissions.values())
            rejected_freezes = tuple(strategy for strategy, submitted in submissions.items() if not submitted)
        else:
            offer = self._task_lanes[scheduled.task].offer(scheduled)
            accepted = offer is not LatestWinsOffer.REJECTED
        if not accepted:
            self._dependencies.cadence.record_submission(scheduled, accepted=False, at=scheduled.scheduled_at)
        for strategy in rejected_freezes:
            self._dependencies.cadence.record_point_result(
                scheduled.scheduled_at.date().isoformat(),
                scheduled.schedule_point or SchedulePoint.AFTERNOON_FREEZE,
                strategy,
                SchedulePointResult.RETRY,
                at=scheduled.scheduled_at,
            )

    def _periodic_task_inflight(self, scheduled: ScheduledPipelineTask) -> bool:
        if scheduled.task not in PERIODIC_TASKS or scheduled.schedule_point is not None:
            return False
        lane = (
            self._lanes[Strategy.LONG].status()
            if scheduled.task is PipelineTask.LONG_QUOTES
            else self._score_lane.status()
        )
        return lane.running or lane.pending

    def _submit_scoring(self, scheduled: ScheduledPipelineTask) -> bool:
        with self._lock:
            return self._score_lane.offer(scheduled) in {LatestWinsOffer.ACCEPTED, LatestWinsOffer.REPLACED}

    def _discard_score_wave(self, scheduled: ScheduledPipelineTask) -> None:
        if scheduled.round_sequence is not None:
            self._dependencies.rounds.finish(self._dependencies.round_profile, scheduled.round_sequence)

    def _process_score_wave(self, scheduled: ScheduledPipelineTask) -> None:
        if scheduled.round_sequence is not None:
            self._process_recommendation_round(scheduled)
            return
        self._execute_score_pair(scheduled)

    def _execute_score_pair(
        self,
        scheduled: ScheduledPipelineTask,
        *,
        round_sequence: int | None = None,
    ) -> None:
        try:
            with SessionGuard(scheduled.session):
                pass
            now = ShanghaiNow(self._dependencies.clock.now())
            if now.date() != scheduled.scheduled_at.date() or now.time() >= wall_time(15, 0):
                return
            freeze = FreezeAt(scheduled.scheduled_at, is_trading_day=True)
            strategies = self._due_strategies(freeze, None, scheduled.scheduled_at)
            if not {Strategy.TOMORROW, Strategy.D25}.issubset(strategies):
                return
            requests = tuple(
                replace(
                    self._scheduled_request(strategy, scheduled.scheduled_at, CyclePhase(strategy, freeze.phase)),
                    session=scheduled.session,
                    round_started_monotonic=scheduled.round_started_monotonic,
                    round_deadline_monotonic=scheduled.round_deadline_monotonic,
                )
                for strategy in (Strategy.TOMORROW, Strategy.D25)
            )
            if round_sequence is None:
                self._cycles.execute_pair((requests[0], requests[1]))
            else:
                self._cycles.execute_round_pair((requests[0], requests[1]), round_sequence=round_sequence)
        except StaleSessionError:
            return

    def _process_recommendation_round(self, scheduled: ScheduledPipelineTask) -> None:
        sequence = scheduled.round_sequence
        if sequence is None:
            raise ValueError("recommendation round identity is missing")
        rounds = self._dependencies.rounds
        profile = self._dependencies.round_profile
        tasks = {
            task.task: replace(
                task,
                session=scheduled.session,
                round_sequence=sequence,
                round_started_monotonic=scheduled.round_started_monotonic,
                round_deadline_monotonic=scheduled.round_deadline_monotonic,
                round_deadline=scheduled.round_deadline,
            )
            for task in scheduled.round_tasks
        }
        if PipelineTask.FULL_MARKET in tasks and PipelineTask.CANDIDATE_QUOTES not in tasks:
            tasks[PipelineTask.CANDIDATE_QUOTES] = replace(
                scheduled, task=PipelineTask.CANDIDATE_QUOTES, round_tasks=(),
            )
        changed = PipelineTask.SCORE in tasks
        try:
            scope = self._dependencies.run_scope
            if scope is not None and scope.preview is not None:
                self._preview.execute(scope.preview, scheduled)
                return
            for task in _DATA_LANES:
                if task not in tasks or task is PipelineTask.CANDIDATE_QUOTES:
                    continue
                if rounds.cancelled(sequence):
                    return
                with SessionGuard(scheduled.session):
                    pass
                now = ShanghaiNow(self._dependencies.clock.now())
                if now.date() != scheduled.scheduled_at.date():
                    return
                if now.time() >= wall_time(15, 0) and task is not PipelineTask.TOPK_QUOTES:
                    continue
                request = tasks[task]
                self._dependencies.cadence.record_submission(request, accepted=True, at=now)
                outcome_changed = self._execute_pipeline_task(request)
                if outcome_changed is None:
                    if task in {PipelineTask.FULL_MARKET, PipelineTask.CANDIDATE_QUOTES}:
                        return
                    continue
                changed = changed or (outcome_changed and task in _SCORING_INPUT_TASKS)
            # Finish slow auxiliary inputs across profiles before refreshing the
            # final candidate quote cut, so their wait does not age that cut.
            if not rounds.prepared(profile, sequence):
                self._record_failure("refresh", "round_collection_timeout")
                return
            candidate = tasks.get(PipelineTask.CANDIDATE_QUOTES)
            if candidate is not None:
                with SessionGuard(scheduled.session):
                    pass
                now = ShanghaiNow(self._dependencies.clock.now())
                if now.date() != scheduled.scheduled_at.date() or now.time() >= wall_time(15, 0):
                    return
                self._dependencies.cadence.record_submission(candidate, accepted=True, at=now)
                candidate_changed = self._execute_pipeline_task(candidate)
                if candidate_changed is None:
                    return
                changed = changed or candidate_changed
            if not rounds.collected(profile, sequence):
                self._record_failure("refresh", "round_collection_timeout")
                return
            if changed:
                self._execute_score_pair(
                    replace(
                        scheduled,
                        scheduled_at=ShanghaiNow(self._dependencies.clock.now()),
                        round_sequence=None,
                        round_tasks=(),
                    ),
                    round_sequence=sequence,
                )
        except StaleSessionError:
            return
        finally:
            rounds.finish(profile, sequence)

    def submit_cycle(self, request: CycleRequest) -> LatestWinsOffer:
        with self._lock:
            if not self._running:
                return LatestWinsOffer.REJECTED
        scope = self._dependencies.run_scope
        if scope is not None and (
            not scope.resolved
            or (scope.is_preview and (request.preview_context != scope.preview or request.strategy is Strategy.LONG))
        ):
            return LatestWinsOffer.REJECTED
        session = request.session
        if session is None:
            session = self._session.capture()
            request = replace(request, session=session)
        if request.trade_date != session.trade_date:
            return LatestWinsOffer.REJECTED
        try:
            with SessionGuard(request.session):
                return self._lanes[request.strategy].offer(request)
        except StaleSessionError:
            return LatestWinsOffer.REJECTED

    def _process_pipeline_task(self, scheduled: ScheduledPipelineTask) -> None:
        try:
            with SessionGuard(scheduled.session):
                pass
            self._execute_pipeline_task(scheduled)
        except StaleSessionError:
            return

    def _execute_pipeline_task(self, scheduled: ScheduledPipelineTask) -> bool | None:
        if scheduled.task is PipelineTask.CLOSE_QUOTES:
            self._close_control.process_close_recovery(scheduled)
            return None
        if scheduled.task is PipelineTask.TOPK_QUOTES:
            return False
        selected_codes: tuple[str, ...] = ()
        request = PipelineTaskRequest(
            scheduled.task,
            ShanghaiNow(self._dependencies.clock.now()),
            selected_codes,
            session=scheduled.session,
            round_deadline=scheduled.round_deadline,
        )
        try:
            outcome = self._dependencies.data.refresh_task(request)
        except DataRefreshPendingError:
            with SessionGuard(scheduled.session):
                self._record_pipeline_result(scheduled, SchedulePointResult.RETRY)
            return None
        except DataRefreshUnavailableError as exc:
            with SessionGuard(scheduled.session):
                self._record_failure("refresh", FailureCode(exc, "refresh_unavailable"), task=scheduled.task)
                self._record_pipeline_result(scheduled, SchedulePointResult.RETRY)
            return None
        with SessionGuard(scheduled.session):
            with self._lock:
                self._resolve_issues_locked(stages=frozenset({"refresh"}), task=scheduled.task)
            self._record_pipeline_result(scheduled, SchedulePointResult.COMPLETED)
        return outcome.changed

    def _record_pipeline_result(
        self,
        scheduled: ScheduledPipelineTask,
        result: SchedulePointResult,
    ) -> None:
        if scheduled.schedule_point is None:
            return
        self._dependencies.cadence.record_results(
            scheduled,
            {"-": result},
            at=ShanghaiNow(self._dependencies.clock.now()),
        )

    def wait_idle(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        waiters = (
            *(lane.wait_idle for lane in self._task_lanes.values()),
            self._score_lane.wait_idle,
            *(self._lanes[strategy].wait_idle for strategy in Strategy),
            *(self._hybrid_lanes[strategy].wait_idle for strategy in (Strategy.TOMORROW, Strategy.D25)),
        )
        for wait_idle in waiters:
            if not wait_idle(max(0.0, deadline - time.monotonic())):
                return False
        while self._control.status().inflight > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return False
            threading.Event().wait(min(0.01, remaining))
        if not self._research.wait_until_idle(max(0.0, deadline - time.monotonic())):
            return False
        return self._dependencies.observer.wait_idle(max(0.0, deadline - time.monotonic()))

    def stop(
        self,
        deadline: ShutdownDeadline,
    ) -> ShutdownReport:
        with self._lock:
            if self._stopped:
                return self._shutdown_report or ShutdownReport.from_steps(deadline, ())
            self._running = False
            self._stopped = True
        self._dependencies.rounds.withdraw(self._dependencies.round_profile)
        self._research.stop(wait=False, deadline=deadline)
        self._dependencies.observer.close()
        for task_lane in self._task_lanes.values():
            task_lane.close()
        self._score_lane.close()
        for strategy_lane in self._lanes.values():
            strategy_lane.close()
        for hybrid_lane in self._hybrid_lanes.values():
            hybrid_lane.close()
        steps: list[ShutdownStep] = [self._control.stop(wait=True, cancel_futures=False, deadline=deadline)]
        steps.extend(lane.stop(deadline=deadline) for lane in self._task_lanes.values())
        steps.append(self._score_lane.stop(deadline=deadline))
        steps.extend(self._lanes[strategy].stop(deadline=deadline) for strategy in Strategy)
        steps.extend(
            self._hybrid_lanes[strategy].stop(deadline=deadline) for strategy in (Strategy.TOMORROW, Strategy.D25)
        )
        steps.append(self._dependencies.observer.stop(deadline=deadline))
        steps.append(self._research.stop(wait=True, deadline=deadline))
        report = ShutdownReport.from_steps(deadline, steps, forced=deadline.expired)
        with self._lock:
            self._shutdown_report = report
        return report

    def status(self) -> SchedulerRuntimeStatus:
        control = self._control.status()
        session = self._session.status()
        lanes = tuple(self._lanes[strategy].status() for strategy in Strategy)
        hybrid_lanes = tuple(self._hybrid_lanes[strategy].status() for strategy in (Strategy.TOMORROW, Strategy.D25))
        task_lanes = (*tuple(lane.status() for lane in self._task_lanes.values()), self._score_lane.status())
        preview = self._preview.status()
        preview_context = self._dependencies.run_scope.preview if self._dependencies.run_scope else None
        with self._lock:
            issues = self._issues.snapshot()
            if preview_context is not None and preview.state == "ready":
                if any(lane.running or lane.pending for lane in hybrid_lanes):
                    preview = replace(preview, state="running")
                elif self._review_failure_count:
                    preview = replace(preview, state="failed", error=issues.last_error_code or "review_unavailable")
            elif preview.state == "failed" and preview.error == "preview_freeze_unavailable":
                preview = replace(preview, error=issues.last_error_code or preview.error)
            return SchedulerRuntimeStatus(
                running=self._running,
                phase=self._phase,
                config_version=self._config_version,
                lanes=lanes,
                hybrid_lanes=hybrid_lanes,
                task_lanes=task_lanes,
                cadence=self._dependencies.cadence.status(),
                observer=self._dependencies.observer.status(),
                deepseek=self._dependencies.reviews.runtime_contract,
                company_research=self._research.status(),
                control_running=control.running,
                control_inflight=control.inflight,
                control_rejected_count=control.rejected_count,
                refresh_failure_count=self._refresh_failure_count,
                freeze_failure_count=self._freeze_failure_count,
                review_failure_count=self._review_failure_count,
                overlay_publish_count=self._overlay_publish_count,
                overlay_failure_count=self._overlay_failure_count,
                local_publish_count=self._local_publish_count,
                hybrid_publish_count=self._hybrid_publish_count,
                publish_rejection_count=self._publish_rejection_count,
                observer_rejection_count=self._observer_rejection_count,
                freeze_completed_count=self._freeze_completed_count,
                seal_failure_count=self._seal_failure_count,
                settlement_completed_count=self._settlement_completed_count,
                settlement_failure_count=self._settlement_failure_count,
                last_error_code=issues.last_error_code,
                strategy_error_codes=issues.strategy_error_codes,
                recent_errors=issues.recent_errors,
                input_quality=self._dependencies.freezes.input_quality_status(),
                preview_context=preview_context,
                preview_state=preview.state if preview_context is not None else None,
                preview_error=preview.error if preview_context is not None else None,
                session=session,
                publication_io=self._dependencies.publication_io.snapshots()
                if self._dependencies.publication_io is not None
                else (),
                calendar=TradingCalendarRuntimeStatus(
                    state=self._calendar_state,
                    trade_date=self._calendar_trade_date,
                    is_trading_day=self._calendar_is_trading_day,
                    consecutive_failure_count=self._calendar_consecutive_failure_count,
                    next_retry_at=self._calendar_next_retry_at,
                ),
            )

    def _scheduled_request(self, strategy: Strategy, observed_at: datetime, phase: str) -> CycleRequest:
        current = self._dependencies.index.snapshot(strategy).current
        current_sequence = current.sequence if current is not None else 0
        with self._lock:
            sequence = self._sequences[strategy] + 1
            while sequence <= current_sequence:
                sequence += 2
            self._sequences[strategy] = sequence + 1
        deadline_time = wall_time(15, 0)
        review_deadline = datetime.combine(observed_at.date(), deadline_time, tzinfo=SHANGHAI)
        preview = self._dependencies.run_scope.preview if self._dependencies.run_scope else None
        if preview is not None:
            review_deadline = observed_at + timedelta(seconds=TaskExecutionBudgetSeconds(PipelineTask.SCORE) or 15.0)
        allow_review = strategy is not Strategy.LONG and observed_at < review_deadline
        return CycleRequest(
            strategy=strategy,
            trade_date=observed_at.date(),
            observed_at=observed_at,
            phase=phase,
            sequence=sequence,
            input_version=f"schedule:{strategy.value}:{observed_at:%Y%m%dT%H%M%S%f}",
            allow_review=allow_review,
            review_deadline=review_deadline,
            session=self._session.capture(),
            preview_context=preview,
        )

    def _record_publish(
        self,
        *,
        hybrid: bool,
        event: FreezeCommitted | None,
        identity: FreezeIdentity,
        publish_event: bool = True,
    ) -> None:
        strategy = identity.strategy
        with self._lock:
            if hybrid:
                self._hybrid_publish_count += 1
            else:
                self._local_publish_count += 1
            self._resolve_issues_locked(
                strategy=strategy,
                stages=frozenset({"refresh", "freeze", "review", "publish"}),
            )
        if event is not None and publish_event:
            try:
                assert isinstance(identity, ScoredFreeze)
                with ObservePublicationIo(
                    self._dependencies.publication_io, "freeze_event", PublicationIoTarget.freeze(identity)
                ) as receipt:
                    self._dependencies.publish_freeze(event)
                    receipt.output_version = event.freeze_version
            except (RuntimeError, TypeError, ValueError) as exc:
                self._record_failure("publish", f"freeze_event:{type(exc).__name__}", strategy)
        if isinstance(identity, ScoredFreeze) and identity.preview_context is not None:
            return
        observation = None
        if event is not None:
            observation = FreezeObservation(
                event, None, audit_factory=self._dependencies.freezes.research_audit_factory(event.freeze_version)
            )
        if observation is not None:
            assert isinstance(identity, ScoredFreeze)
            with ObservePublicationIo(
                self._dependencies.publication_io, "observer_enqueue", PublicationIoTarget.freeze(identity)
            ) as receipt:
                offered = self._dependencies.observer.offer(observation)
                receipt.state = StageState.READY if offered else StageState.FAILED
                receipt.reason = "enqueued" if offered else "observer_capacity_rejected"
                receipt.output_version = observation.event.freeze_version if offered else None
                if not offered:
                    with self._lock:
                        self._observer_rejection_count += 1

    def _record_publish_rejection(self, reason: str, strategy: Strategy) -> None:
        with self._lock:
            self._publish_rejection_count += 1
        if reason in {"freeze_closed", "freeze_sealed"}:
            return
        self._record_failure("publish", reason, strategy)

    def _after_close_recovery_strategies(self, observed_at: datetime) -> tuple[Strategy, ...]:
        strategies = list(self._missing_after_close_scored_strategies(observed_at))
        long_current = self._dependencies.index.snapshot(Strategy.LONG).current
        if not isinstance(long_current, LongProjection) or long_current.trade_date != observed_at.date():
            strategies.append(Strategy.LONG)
        return tuple(strategies)

    def _missing_after_close_scored_strategies(self, observed_at: datetime) -> tuple[Strategy, ...]:
        return tuple(
            strategy
            for strategy in (Strategy.TOMORROW, Strategy.D25)
            if (formal := self._dependencies.index.snapshot(strategy).formal) is None
            or formal.trade_date != observed_at.date()
        )

    def _due_strategies(
        self,
        freeze: ScheduleFreeze,
        schedule_point: SchedulePoint | None,
        observed_at: datetime,
    ) -> tuple[Strategy, ...]:
        if freeze.phase is MarketPhase.AFTER_CLOSE and schedule_point is not SchedulePoint.CLOSE_QUOTES:
            return self._after_close_recovery_strategies(observed_at)
        strategies: tuple[Strategy, ...] = ()
        if freeze.should_score:
            strategies = (Strategy.TOMORROW, Strategy.D25)
        if freeze.phase is not MarketPhase.AFTER_CLOSE and freeze.should_refresh_market:
            strategies = (*strategies, Strategy.LONG)
        return strategies

    def _record_control_success(self, stage: str, strategy: Strategy | None) -> None:
        with self._lock:
            if stage == "seal":
                self._freeze_completed_count += 1
            elif stage == "settlement":
                self._settlement_completed_count += 1
            self._resolve_issues_locked(strategy=strategy, stages=frozenset({stage}))

    def _record_failure(
        self,
        stage: str,
        code: str,
        strategy: Strategy | None = None,
        *,
        task: PipelineTask | None = None,
    ) -> None:
        occurred_at = ShanghaiNow(self._dependencies.clock.now())
        with self._lock:
            if stage == "refresh":
                self._refresh_failure_count += 1
            elif stage == "freeze":
                self._freeze_failure_count += 1
            elif stage == "review":
                self._review_failure_count += 1
            elif stage == "overlay":
                self._overlay_failure_count += 1
            elif stage == "seal":
                self._seal_failure_count += 1
            elif stage == "settlement":
                self._settlement_failure_count += 1
            qualified = f"{stage}:{code}"
            self._issues.record(qualified, stage, strategy, occurred_at, task=task)

    def _record_overlay_success(self, strategy: Strategy, *, published: bool) -> None:
        with self._lock:
            if published:
                self._overlay_publish_count += 1
            self._resolve_issues_locked(strategy=strategy, stages=frozenset({"overlay"}))

    def _resolve_issues_locked(
        self,
        *,
        strategy: Strategy | None = None,
        stages: frozenset[str] | None = None,
        task: PipelineTask | None = None,
    ) -> None:
        self._issues.resolve(
            ShanghaiNow(self._dependencies.clock.now()),
            strategy=strategy,
            stages=stages,
            task=task,
        )


def _hybrid_order_key(request: HybridUpgradeRequest) -> int:
    return CycleOrderKey(request.cycle)


__all__ = ["RuntimeIssue", "SchedulerRuntimeStatus", "SchedulerRuntime"]
