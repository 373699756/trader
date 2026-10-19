from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from datetime import time as wall_time
from typing import Literal

from trader.infra.shutdown import ShutdownDeadline, ShutdownReport, ShutdownStep
from trader.recommendation.application.pipeline.freeze_publish.decision_events import (
    DecisionCommitted,
    DecisionObservation,
)
from trader.recommendation.application.pipeline.freeze_publish.overlay_publisher import DecisionOverlayRefresher
from trader.recommendation.application.pipeline.freeze_publish.publication_io import (
    ObservePublicationIo,
    PublicationIoTarget,
)
from trader.recommendation.application.ports.runtime import (
    CycleRequest,
    DataRefreshUnavailableError,
    PipelineTaskRequest,
    RefreshOutcome,
    TradingCalendarUnavailableError,
)
from trader.recommendation.application.runtime.cadence import (
    PERIODIC_TASKS,
    PipelineTask,
    ScheduledPipelineTask,
    SchedulePointResult,
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
from trader.recommendation.application.runtime.runtime_dependencies import RuntimeDependencies
from trader.recommendation.application.runtime.runtime_issues import RuntimeIssue, RuntimeIssueIndex
from trader.recommendation.application.runtime.schedule import (
    SHANGHAI,
    DecisionAt,
    MarketPhase,
    ScheduleDecision,
    SchedulePoint,
    ShanghaiNow,
)
from trader.recommendation.application.runtime.schedule_requests import (
    CycleCorrelationId,
    CycleOrderKey,
    CyclePhase,
    FailureCode,
    PipelineLane,
    PipelineTaskCorrelationId,
    PipelineTaskOrderKey,
)
from trader.recommendation.application.runtime.status import SchedulerRuntimeStatus, TradingCalendarRuntimeStatus
from trader.recommendation.application.runtime.trading_session import SessionGuard, StaleSessionError, TradingSession
from trader.recommendation.domain.evidence.pipeline import StageState
from trader.recommendation.domain.publication.decision_identity import (
    DecisionIdentity,
    DisplayQuoteCodes,
    IdentityCodes,
    LongProjection,
    ScoredDecision,
)
from trader.recommendation.domain.publication.models import Strategy

_DATA_TASKS = (
    PipelineTask.FULL_MARKET,
    PipelineTask.CANDIDATE_QUOTES,
    PipelineTask.TOPK_QUOTES,
    PipelineTask.INTRADAY_TAIL,
    PipelineTask.INDUSTRY_HEAT,
    PipelineTask.RESEARCH_SNAPSHOT,
    PipelineTask.REFERENCE_DATA,
    PipelineTask.FINAL_CANDIDATE_QUOTES,
    PipelineTask.CLOSE_QUOTES,
    PipelineTask.CURRENT_QUOTES,
)
_DATA_LANES = tuple(
    task
    for task in _DATA_TASKS
    if task not in {PipelineTask.FINAL_CANDIDATE_QUOTES, PipelineTask.CLOSE_QUOTES, PipelineTask.CURRENT_QUOTES}
)
_SCORING_INPUT_TASKS = frozenset(
    {
        PipelineTask.CANDIDATE_QUOTES,
        PipelineTask.FINAL_CANDIDATE_QUOTES,
        PipelineTask.RESEARCH_SNAPSHOT,
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
        shutdown_timeout_seconds: float = 30.0,
    ) -> None:
        if not config_version:
            raise ValueError("scheduler runtime config version must not be empty")
        contract = dependencies.reviews.runtime_contract
        if contract.daily_physical_limit != 168 or not contract.shared_cache or not contract.shared_single_flight:
            raise ValueError("scheduler runtime requires the shared 168-request DeepSeek boundary")
        self._dependencies = dependencies
        self._overlay_refresher = DecisionOverlayRefresher(
            dependencies.index,
            dependencies.decisions,
            dependencies.publish_overlay,
            FailureCode,
        )
        self._research = dependencies.research_factory()
        self._config_version = config_version
        self._shutdown_timeout_seconds = max(0.1, shutdown_timeout_seconds)
        self._lock = threading.RLock()
        self._session = TradingSession(dependencies.clock.now, dependencies.monotonic)
        self._planned_generation = 0
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
            for task in _DATA_LANES
        }
        self._score_lane = LatestWinsWorker(
            "trader-task-score-wave",
            self._process_score_wave,
            order_key=PipelineTaskOrderKey,
            telemetry=LatestWinsTelemetry(
                dependencies.latency,
                PipelineTaskCorrelationId,
                lambda request: f"score-wave:{request.task.value}",
            ),
        )
        self._refresh_failure_count = 0
        self._decision_failure_count = 0
        self._review_failure_count = 0
        self._overlay_publish_count = 0
        self._overlay_failure_count = 0
        self._local_publish_count = 0
        self._hybrid_publish_count = 0
        self._publish_rejection_count = 0
        self._observer_rejection_count = 0
        self._freeze_completed_count = 0
        self._freeze_failure_count = 0
        self._settlement_completed_count = 0
        self._settlement_failure_count = 0
        self._issues = RuntimeIssueIndex()
        self._midday_long_handoff_date: date | None = None
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
            overlay_refresher=self._overlay_refresher,
            close_control=self._close_control,
            research=self._research,
            hooks=CycleExecutionHooks(
                lambda strategy: self._lanes[strategy],
                lambda strategy: self._hybrid_lanes[strategy],
                self._record_failure,
                lambda strategy, published: self._record_overlay_success(strategy, published=published),
                lambda hybrid, event, identity: self._record_publish(hybrid=hybrid, event=event, identity=identity),
                self._record_publish_rejection,
                self._record_long_handoff,
            ),
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
            raise RuntimeError("decision observer did not start")
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
        for task in _DATA_LANES:
            if not self._task_lanes[task].start():
                raise RuntimeError(f"{task.value} task lane did not start")
        if not self._score_lane.start():
            raise RuntimeError("score wave lane did not start")

    def _abort_start(self) -> None:
        deadline = ShutdownDeadline.start(self._shutdown_timeout_seconds)
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
        session = self._session.capture()
        if session.generation != self._planned_generation:
            self._dependencies.cadence.rotate_session(observed_at, reason=self._session.status().discontinuity_reason)
            self._close_control.rotate_session()
            self._planned_generation = session.generation
        is_trading_day, retry_delay = self._calendar_decision(observed_at)
        if is_trading_day is None:
            with self._lock:
                self._phase = MarketPhase.CLOSED
            return retry_delay
        batch = self._dependencies.cadence.plan(observed_at, is_trading_day=is_trading_day)
        with self._lock:
            self._phase = (
                batch.tasks[0].phase
                if batch.tasks
                else DecisionAt(
                    observed_at,
                    is_trading_day=is_trading_day,
                ).phase
            )
        for scheduled in batch.tasks:
            if not self._periodic_task_inflight(scheduled):
                self._dispatch_pipeline_task(replace(scheduled, session=session))
        return batch.next_delay_seconds

    def _calendar_decision(self, observed_at: datetime) -> tuple[bool | None, float]:
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
        completed_immediately = False
        rejected_freezes: tuple[str, ...] = ()
        if scheduled.task is PipelineTask.SCORE:
            accepted = self._submit_scoring(scheduled)
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
        elif scheduled.task is PipelineTask.CHECKPOINT:
            submissions = {
                strategy: self._close_control.submit_checkpoint(
                    Strategy(strategy),
                    scheduled.scheduled_at,
                    scheduled=scheduled,
                )
                for strategy in scheduled.freeze_strategies
            }
            accepted = any(submissions.values())
            rejected_freezes = tuple(strategy for strategy, submitted in submissions.items() if not submitted)
        elif scheduled.task is PipelineTask.DEEPSEEK_CUTOFF:
            accepted = True
            completed_immediately = True
        else:
            offer = self._task_lanes[PipelineLane(scheduled.task)].offer(scheduled)
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
        if completed_immediately:
            self._dependencies.cadence.record_results(
                scheduled,
                {"-": SchedulePointResult.COMPLETED},
                at=scheduled.scheduled_at,
            )

    def _periodic_task_inflight(self, scheduled: ScheduledPipelineTask) -> bool:
        if scheduled.task not in PERIODIC_TASKS or scheduled.schedule_point is not None:
            return False
        if scheduled.task is PipelineTask.FULL_MARKET:
            candidate = self._task_lanes[PipelineTask.CANDIDATE_QUOTES].status()
            if candidate.running or candidate.pending:
                return True
        lane = (
            self._lanes[Strategy.LONG].status()
            if scheduled.task is PipelineTask.LONG_QUOTES
            else self._task_lanes[PipelineLane(scheduled.task)].status()
        )
        return lane.running or lane.pending

    def _submit_scoring(self, scheduled: ScheduledPipelineTask) -> bool:
        with self._lock:
            state = self._score_lane.status()
            if state.running or state.pending:
                return False
            return self._score_lane.offer(scheduled) is not LatestWinsOffer.REJECTED

    def _process_score_wave(self, scheduled: ScheduledPipelineTask) -> None:
        try:
            with SessionGuard(scheduled.session):
                pass
            decision = DecisionAt(scheduled.scheduled_at, is_trading_day=True)
            strategies = self._due_strategies(decision, None, scheduled.scheduled_at)
            if not {Strategy.TOMORROW, Strategy.D25}.issubset(strategies):
                return
            requests = tuple(
                replace(
                    self._scheduled_request(strategy, scheduled.scheduled_at, CyclePhase(strategy, decision.phase)),
                    session=scheduled.session,
                )
                for strategy in (Strategy.TOMORROW, Strategy.D25)
            )
            self._cycles.execute_pair((requests[0], requests[1]))
        except StaleSessionError:
            return

    def submit_cycle(self, request: CycleRequest) -> LatestWinsOffer:
        with self._lock:
            if not self._running:
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

    def _execute_pipeline_task(self, scheduled: ScheduledPipelineTask) -> None:
        if scheduled.task is PipelineTask.CLOSE_QUOTES:
            self._close_control.process_close_recovery(scheduled)
            return
        selected_codes = (
            self._selected_overlay_codes(scheduled.phase) if scheduled.task is PipelineTask.TOPK_QUOTES else ()
        )
        request = PipelineTaskRequest(
            scheduled.task,
            ShanghaiNow(self._dependencies.clock.now()),
            selected_codes,
            session=scheduled.session,
        )
        try:
            outcome = self._dependencies.data.refresh_task(request)
        except DataRefreshUnavailableError as exc:
            with SessionGuard(scheduled.session):
                self._record_failure("refresh", FailureCode(exc, "refresh_unavailable"), task=scheduled.task)
                self._record_pipeline_result(scheduled, SchedulePointResult.RETRY)
            return
        with SessionGuard(scheduled.session):
            self._after_successful_data_refresh(scheduled, outcome, observed_at=request.observed_at)
            with self._lock:
                self._resolve_issues_locked(stages=frozenset({"refresh"}), task=scheduled.task)
            self._record_pipeline_result(scheduled, SchedulePointResult.COMPLETED)

    def _after_successful_data_refresh(
        self,
        scheduled: ScheduledPipelineTask,
        outcome: RefreshOutcome,
        *,
        observed_at: datetime,
    ) -> None:
        if scheduled.task is PipelineTask.TOPK_QUOTES and outcome.changed:
            self._refresh_selected_overlays(scheduled, observed_at)
        elif scheduled.task is PipelineTask.FULL_MARKET and scheduled.phase is MarketPhase.MIDDAY:
            for strategy in self._midday_recovery_strategies(scheduled.scheduled_at):
                if strategy is Strategy.LONG:
                    continue
                self.submit_cycle(
                    replace(
                        self._scheduled_request(strategy, scheduled.scheduled_at, "midday_recovery"),
                        session=scheduled.session,
                    )
                )
        if scheduled.task in _SCORING_INPUT_TASKS and outcome.changed:
            self._trigger_scoring_after_input()

    def _trigger_scoring_after_input(self) -> None:
        completed_at = ShanghaiNow(self._dependencies.clock.now())
        is_trading_day, _retry_delay = self._calendar_decision(completed_at)
        if is_trading_day is None:
            return
        scheduled = self._dependencies.cadence.plan_score_after_input(
            completed_at,
            is_trading_day=is_trading_day,
        )
        if scheduled is not None:
            self._dispatch_pipeline_task(scheduled)

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

    def _selected_overlay_codes(self, phase: MarketPhase) -> tuple[str, ...]:
        codes: list[str] = []
        visible_six = phase in {
            MarketPhase.MORNING_OBSERVE,
            MarketPhase.MORNING_MAIN,
            MarketPhase.MORNING_LATE,
            MarketPhase.AFTERNOON,
            MarketPhase.FINAL_REVIEW,
            MarketPhase.DEEPSEEK_CUTOFF,
            MarketPhase.FINAL_QUOTE,
        }
        for strategy in (Strategy.TOMORROW, Strategy.D25):
            snapshot = self._dependencies.index.snapshot(strategy)
            identity = snapshot.formal.decision if snapshot.formal is not None else snapshot.current
            if isinstance(identity, ScoredDecision):
                codes.extend(sorted(DisplayQuoteCodes(identity) if visible_six else IdentityCodes(identity)))
        return tuple(dict.fromkeys(codes))

    def _refresh_selected_overlays(self, scheduled: ScheduledPipelineTask, observed_at: datetime) -> None:
        for strategy in (Strategy.TOMORROW, Strategy.D25):
            snapshot = self._dependencies.index.snapshot(strategy)
            if (
                not isinstance(snapshot.current, ScoredDecision)
                or snapshot.current.trade_date != scheduled.scheduled_at.date()
            ):
                continue
            request = replace(
                self._scheduled_request(strategy, observed_at, "quote_overlay"), session=scheduled.session
            )
            for outcome in self._overlay_refresher.refresh(request):
                if outcome.status == "failed":
                    self._record_failure("overlay", outcome.error_code, outcome.strategy)
                elif outcome.status != "skipped":
                    self._record_overlay_success(outcome.strategy, published=outcome.status == "published")
            break

    def wait_idle(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        waiters = (
            *(self._task_lanes[task].wait_idle for task in _DATA_LANES),
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
        timeout_seconds: float | ShutdownDeadline = 15.0,
        *,
        deadline: ShutdownDeadline | None = None,
    ) -> ShutdownReport:
        if isinstance(timeout_seconds, ShutdownDeadline):
            deadline = deadline or timeout_seconds
        else:
            deadline = deadline or ShutdownDeadline.start(timeout_seconds)
        with self._lock:
            if self._stopped:
                return self._shutdown_report or ShutdownReport.from_steps(deadline, ())
            self._running = False
            self._stopped = True
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
        steps.extend(self._task_lanes[task].stop(deadline=deadline) for task in _DATA_LANES)
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
        task_lanes = (*tuple(self._task_lanes[task].status() for task in _DATA_LANES), self._score_lane.status())
        with self._lock:
            issues = self._issues.snapshot()
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
                decision_failure_count=self._decision_failure_count,
                review_failure_count=self._review_failure_count,
                overlay_publish_count=self._overlay_publish_count,
                overlay_failure_count=self._overlay_failure_count,
                local_publish_count=self._local_publish_count,
                hybrid_publish_count=self._hybrid_publish_count,
                publish_rejection_count=self._publish_rejection_count,
                observer_rejection_count=self._observer_rejection_count,
                freeze_completed_count=self._freeze_completed_count,
                freeze_failure_count=self._freeze_failure_count,
                settlement_completed_count=self._settlement_completed_count,
                settlement_failure_count=self._settlement_failure_count,
                last_error_code=issues.last_error_code,
                strategy_error_codes=issues.strategy_error_codes,
                recent_errors=issues.recent_errors,
                input_quality=self._dependencies.decisions.input_quality_status(),
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
        allow_review = strategy is not Strategy.LONG and phase != "midday_recovery" and observed_at < review_deadline
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
        )

    def _record_publish(
        self,
        *,
        hybrid: bool,
        event: DecisionCommitted | None,
        identity: DecisionIdentity,
    ) -> None:
        strategy = identity.strategy
        with self._lock:
            if hybrid:
                self._hybrid_publish_count += 1
            else:
                self._local_publish_count += 1
            self._resolve_issues_locked(
                strategy=strategy,
                stages=frozenset({"refresh", "decision", "review", "publish"}),
            )
        if event is not None:
            try:
                assert isinstance(identity, ScoredDecision)
                with ObservePublicationIo(
                    self._dependencies.publication_io, "decision_event", PublicationIoTarget.decision(identity)
                ) as receipt:
                    self._dependencies.publish_decision(event)
                    receipt.output_version = event.decision_version
            except (RuntimeError, TypeError, ValueError) as exc:
                self._record_failure("publish", f"decision_event:{type(exc).__name__}", strategy)
        observation = None
        if event is not None:
            observation = DecisionObservation(
                event, None, audit_factory=self._dependencies.decisions.research_audit_factory(event.decision_version)
            )
        if observation is not None:
            assert isinstance(identity, ScoredDecision)
            with ObservePublicationIo(
                self._dependencies.publication_io, "observer_enqueue", PublicationIoTarget.decision(identity)
            ) as receipt:
                offered = self._dependencies.observer.offer(observation)
                receipt.state = StageState.READY if offered else StageState.FAILED
                receipt.reason = "enqueued" if offered else "observer_capacity_rejected"
                receipt.output_version = observation.event.decision_version if offered else None
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
        decision: ScheduleDecision,
        schedule_point: SchedulePoint | None,
        observed_at: datetime,
    ) -> tuple[Strategy, ...]:
        if decision.phase is MarketPhase.AFTER_CLOSE and schedule_point is not SchedulePoint.CLOSE_QUOTES:
            return self._after_close_recovery_strategies(observed_at)
        if decision.phase is MarketPhase.MIDDAY:
            return self._midday_recovery_strategies(observed_at)
        strategies: tuple[Strategy, ...] = ()
        if decision.should_score:
            strategies = (Strategy.TOMORROW, Strategy.D25)
        if decision.phase is not MarketPhase.AFTER_CLOSE and decision.should_refresh_market:
            strategies = (*strategies, Strategy.LONG)
        return strategies

    def _midday_recovery_strategies(self, observed_at: datetime) -> tuple[Strategy, ...]:
        strategies: list[Strategy] = []
        for strategy in (Strategy.TOMORROW, Strategy.D25):
            current = self._dependencies.index.snapshot(strategy).current
            if current is not None and current.trade_date == observed_at.date():
                continue
            if self._dependencies.decisions.has_local_draft(strategy, observed_at.date()):
                continue
            lane = self._lanes[strategy].status()
            if not lane.running and not lane.pending:
                strategies.append(strategy)
        long_current = self._dependencies.index.snapshot(Strategy.LONG).current
        with self._lock:
            long_handed_off = self._midday_long_handoff_date == observed_at.date()
        long_lane = self._lanes[Strategy.LONG].status()
        if (
            (long_current is None or long_current.trade_date != observed_at.date())
            and not long_handed_off
            and not long_lane.running
            and not long_lane.pending
        ):
            strategies.append(Strategy.LONG)
        return tuple(strategies)

    def _record_long_handoff(self, trade_date: date) -> None:
        with self._lock:
            self._midday_long_handoff_date = trade_date

    def _record_control_success(self, stage: str, strategy: Strategy | None) -> None:
        with self._lock:
            if stage == "freeze":
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
            elif stage == "decision":
                self._decision_failure_count += 1
            elif stage == "review":
                self._review_failure_count += 1
            elif stage == "overlay":
                self._overlay_failure_count += 1
            elif stage == "freeze":
                self._freeze_failure_count += 1
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
