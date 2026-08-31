"""Independent V2 scheduling, strategy lanes, publication, and shutdown."""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from datetime import time as wall_time
from typing import Literal, cast

from trader.application.cadence import (
    CadencePlanner,
    CadencePlannerStatus,
    PipelineTask,
    ScheduledPipelineTask,
    SchedulePointResult,
)
from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.decision_events import V2DecisionCommitted
from trader.application.decision_observers import DecisionObserverRuntime, DecisionObserverStatus
from trader.application.decision_overlay_refresh import DecisionOverlayRefresher
from trader.application.latency import LatencyWaterfall
from trader.application.ports.clock import Clock
from trader.application.ports.market import ResearchRefreshResult
from trader.application.ports.runtime_status import V2InputQualityStatus
from trader.application.ports.v2_runtime import (
    SharedDeepSeekRuntimeContract,
    V2CycleRequest,
    V2DataRefreshPort,
    V2DataRefreshUnavailableError,
    V2DecisionBuilderPort,
    V2DecisionUnavailableError,
    V2DeepSeekUpgradePort,
    V2FreezePort,
    V2FreezeUnavailableError,
    V2OverlayPublisher,
    V2PipelineTaskRequest,
    V2RefreshOutcome,
    V2ResearchRuntimeFactoryPort,
    V2ResearchRuntimeStatus,
    V2ReviewUnavailableError,
    V2SettlementPort,
    V2SettlementUnavailableError,
    V2TradingCalendarPort,
)
from trader.application.research_audit import V2DecisionObservation
from trader.application.schedule import (
    SHANGHAI,
    MarketPhase,
    ScheduleDecision,
    SchedulePoint,
    decision_at,
    shanghai_now,
)
from trader.application.shutdown import ShutdownDeadline, ShutdownReport, ShutdownStep
from trader.application.v2_lifecycle import (
    LatestWinsOffer,
    LatestWinsStatus,
    LatestWinsTelemetry,
    LatestWinsWorker,
)
from trader.application.v2_runtime_issues import V2RuntimeIssue, V2RuntimeIssueRegistry
from trader.application.workers import BoundedExecutor
from trader.domain.recommendation.decision_identity import (
    DecisionIdentity,
    LongProjection,
    ScoredDecision,
    identity_codes,
)
from trader.domain.recommendation.models import Strategy

_DATA_TASKS = (
    PipelineTask.FULL_MARKET,
    PipelineTask.CANDIDATE_QUOTES,
    PipelineTask.TOPK_QUOTES,
    PipelineTask.INTRADAY_TAIL,
    PipelineTask.INDUSTRY_HEAT,
    PipelineTask.MARKET_NEWS,
    PipelineTask.STOCK_RISK,
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
        PipelineTask.MARKET_NEWS,
        PipelineTask.STOCK_RISK,
    }
)


@dataclass(frozen=True)
class V2RuntimeDependencies:
    clock: Clock
    calendar: V2TradingCalendarPort
    cadence: CadencePlanner
    data: V2DataRefreshPort
    decisions: V2DecisionBuilderPort
    reviews: V2DeepSeekUpgradePort
    index: UnifiedDecisionIndex
    observer: DecisionObserverRuntime
    freezes: V2FreezePort
    settlement: V2SettlementPort
    research_factory: V2ResearchRuntimeFactoryPort
    publish_decision: Callable[[V2DecisionCommitted], object]
    publish_overlay: V2OverlayPublisher
    latency: LatencyWaterfall = field(default_factory=LatencyWaterfall)


@dataclass(frozen=True)
class _HybridUpgradeRequest:
    local: ScoredDecision
    cycle: V2CycleRequest


@dataclass(frozen=True)
class V2RuntimeStatus:
    running: bool
    phase: MarketPhase
    config_version: str
    lanes: tuple[LatestWinsStatus, ...]
    hybrid_lanes: tuple[LatestWinsStatus, ...]
    task_lanes: tuple[LatestWinsStatus, ...]
    cadence: CadencePlannerStatus
    observer: DecisionObserverStatus
    deepseek: SharedDeepSeekRuntimeContract
    company_research: V2ResearchRuntimeStatus
    control_running: bool
    control_inflight: int
    control_rejected_count: int
    refresh_failure_count: int
    decision_failure_count: int
    review_failure_count: int
    overlay_publish_count: int
    overlay_failure_count: int
    local_publish_count: int
    hybrid_publish_count: int
    publish_rejection_count: int
    observer_rejection_count: int
    freeze_completed_count: int
    freeze_failure_count: int
    settlement_completed_count: int
    settlement_failure_count: int
    last_error_code: str
    strategy_error_codes: tuple[tuple[str, str], ...]
    recent_errors: tuple[V2RuntimeIssue, ...]
    input_quality: tuple[V2InputQualityStatus, ...]


class V2SchedulerRuntime:
    """Scheduler-facing V2 runtime that owns no legacy Pipeline resources."""

    def __init__(
        self,
        dependencies: V2RuntimeDependencies,
        *,
        config_version: str,
        shutdown_timeout_seconds: float = 30.0,
    ) -> None:
        if not config_version:
            raise ValueError("V2 runtime config version must not be empty")
        contract = dependencies.reviews.runtime_contract
        if contract.daily_physical_limit != 168 or not contract.shared_cache or not contract.shared_single_flight:
            raise ValueError("V2 runtime requires the shared 168-request DeepSeek boundary")
        self._dependencies = dependencies
        self._overlay_refresher = DecisionOverlayRefresher(
            dependencies.index,
            dependencies.decisions,
            dependencies.publish_overlay,
            _failure_code,
        )
        self._research = dependencies.research_factory(self._on_research_result)
        self._config_version = config_version
        self._shutdown_timeout_seconds = max(0.1, shutdown_timeout_seconds)
        self._lock = threading.RLock()
        self._running = False
        self._stopped = False
        self._shutdown_report: ShutdownReport | None = None
        self._phase = MarketPhase.CLOSED
        self._sequences = dict.fromkeys(Strategy, 0)
        self._control_pending: set[str] = set()
        self._control_completed: OrderedDict[str, None] = OrderedDict()
        self._control = BoundedExecutor(
            worker_count=2,
            urgent_worker_count=1,
            queue_capacity=4,
            thread_name_prefix="trader-v2-control",
        )
        self._lanes = {
            strategy: LatestWinsWorker(
                f"trader-v2-{strategy.value}",
                self._process_cycle,
                order_key=_cycle_order_key,
                telemetry=LatestWinsTelemetry(
                    dependencies.latency,
                    _cycle_correlation_id,
                    lambda request: f"score:{request.strategy.value}",
                ),
            )
            for strategy in Strategy
        }
        self._hybrid_lanes: dict[Strategy, LatestWinsWorker[_HybridUpgradeRequest]] = {
            strategy: LatestWinsWorker(
                f"trader-v2-hybrid-{strategy.value}",
                self._process_hybrid,
                order_key=_hybrid_order_key,
                telemetry=LatestWinsTelemetry(
                    dependencies.latency,
                    lambda request: f"hybrid:{_cycle_correlation_id(request.cycle)}",
                    lambda request: f"hybrid:{request.cycle.strategy.value}",
                ),
            )
            for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)
        }
        self._task_lanes: dict[PipelineTask, LatestWinsWorker[ScheduledPipelineTask]] = {
            task: LatestWinsWorker(
                f"trader-v2-task-{task.value}",
                self._process_pipeline_task,
                order_key=_pipeline_task_order_key,
                telemetry=LatestWinsTelemetry(
                    dependencies.latency,
                    _pipeline_task_correlation_id,
                    lambda request: f"data:{request.task.value}",
                ),
            )
            for task in _DATA_LANES
        }
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
        self._issues = V2RuntimeIssueRegistry()
        self._midday_long_handoff_date: date | None = None

    def start(self) -> bool:
        with self._lock:
            if self._running:
                return False
            if self._stopped:
                raise RuntimeError("V2 scheduler runtime cannot restart after stop")
            self._running = True
        try:
            self._start_components()
        except BaseException:
            self._abort_start()
            raise
        return True

    def _start_components(self) -> None:
        if not self._dependencies.observer.start():
            raise RuntimeError("V2 observer did not start")
        if not self._control.start():
            raise RuntimeError("V2 control executor did not start")
        for task in _DATA_LANES:
            if not self._task_lanes[task].start():
                raise RuntimeError(f"V2 {task.value} task lane did not start")
        for strategy in Strategy:
            if not self._lanes[strategy].start():
                raise RuntimeError(f"V2 {strategy.value} lane did not start")
        for strategy, lane in self._hybrid_lanes.items():
            if not lane.start():
                raise RuntimeError(f"V2 {strategy.value} hybrid lane did not start")
        if not self._research.start():
            raise RuntimeError("V2 research runtime did not start")

    def _abort_start(self) -> None:
        deadline = ShutdownDeadline.start(self._shutdown_timeout_seconds)
        self._research.stop(wait=False, deadline=deadline)
        for task_lane in self._task_lanes.values():
            task_lane.close()
        for strategy_lane in self._lanes.values():
            strategy_lane.close()
        for hybrid_lane in self._hybrid_lanes.values():
            hybrid_lane.close()
        for task_lane in self._task_lanes.values():
            task_lane.stop(deadline=deadline)
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
        observed_at = shanghai_now(at or self._dependencies.clock.now())
        is_trading_day = self._dependencies.calendar.is_trading_day(observed_at.date())
        batch = self._dependencies.cadence.plan(observed_at, is_trading_day=is_trading_day)
        with self._lock:
            self._phase = (
                batch.tasks[0].phase
                if batch.tasks
                else decision_at(
                    observed_at,
                    is_trading_day=is_trading_day,
                ).phase
            )
        for scheduled in batch.tasks:
            self._dispatch_pipeline_task(scheduled)
        phase = decision_at(observed_at, is_trading_day=is_trading_day).phase
        try:
            self._research.offer_due(observed_at, phase, is_trading_day=is_trading_day)
        except (RuntimeError, TypeError, ValueError) as exc:
            self._record_failure("research", _failure_code(exc, "research_offer_failed"), None)
        return batch.next_delay_seconds

    def _dispatch_pipeline_task(self, scheduled: ScheduledPipelineTask) -> None:
        completed_immediately = False
        rejected_freezes: tuple[str, ...] = ()
        if scheduled.task is PipelineTask.SCORE:
            accepted = self._submit_scoring(scheduled)
        elif scheduled.task is PipelineTask.LONG_QUOTES:
            offer = self.submit_cycle(
                self._scheduled_request(Strategy.LONG, scheduled.scheduled_at, scheduled.phase.value)
            )
            accepted = offer is not LatestWinsOffer.REJECTED
        elif scheduled.task is PipelineTask.FREEZE:
            submissions = {
                strategy: self._submit_freeze(
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
                strategy: self._submit_checkpoint(
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
            offer = self._task_lanes[_pipeline_lane(scheduled.task)].offer(scheduled)
            accepted = offer is not LatestWinsOffer.REJECTED
        self._dependencies.cadence.record_submission(
            scheduled,
            accepted=accepted,
            at=scheduled.scheduled_at,
        )
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

    def _submit_scoring(self, scheduled: ScheduledPipelineTask) -> bool:
        decision = decision_at(scheduled.scheduled_at, is_trading_day=True)
        accepted = False
        for strategy in self._due_strategies(decision, None, scheduled.scheduled_at):
            if strategy is Strategy.LONG:
                continue
            phase = _cycle_phase(strategy, decision.phase)
            offer = self.submit_cycle(self._scheduled_request(strategy, scheduled.scheduled_at, phase))
            accepted = accepted or offer is not LatestWinsOffer.REJECTED
        return accepted

    def submit_cycle(self, request: V2CycleRequest) -> LatestWinsOffer:
        with self._lock:
            if not self._running:
                return LatestWinsOffer.REJECTED
        return self._lanes[request.strategy].offer(request)

    def _process_pipeline_task(self, scheduled: ScheduledPipelineTask) -> None:
        if scheduled.task is PipelineTask.CLOSE_QUOTES and not self._missing_after_close_scored_strategies(
            scheduled.scheduled_at
        ):
            self._submit_settlement(scheduled.scheduled_at)
            self._record_pipeline_result(scheduled, SchedulePointResult.COMPLETED)
            return
        selected_codes = self._selected_overlay_codes() if scheduled.task is PipelineTask.TOPK_QUOTES else ()
        request = V2PipelineTaskRequest(
            scheduled.task,
            shanghai_now(self._dependencies.clock.now()),
            selected_codes,
        )
        try:
            outcome = self._dependencies.data.refresh_task(request)
        except V2DataRefreshUnavailableError as exc:
            self._record_failure("refresh", _failure_code(exc, "refresh_unavailable"))
            self._record_pipeline_result(scheduled, SchedulePointResult.RETRY)
            return
        self._after_successful_data_refresh(scheduled, outcome)
        self._record_pipeline_result(scheduled, SchedulePointResult.COMPLETED)

    def _after_successful_data_refresh(
        self,
        scheduled: ScheduledPipelineTask,
        outcome: V2RefreshOutcome,
    ) -> None:
        if scheduled.task is PipelineTask.TOPK_QUOTES and outcome.changed:
            self._refresh_selected_overlays(scheduled)
        elif scheduled.task is PipelineTask.FULL_MARKET and scheduled.phase is MarketPhase.MIDDAY:
            for strategy in self._midday_recovery_strategies(scheduled.scheduled_at):
                if strategy is Strategy.LONG:
                    continue
                self.submit_cycle(self._scheduled_request(strategy, scheduled.scheduled_at, "midday_recovery"))
        elif scheduled.task is PipelineTask.CLOSE_QUOTES:
            for strategy in self._after_close_recovery_strategies(scheduled.scheduled_at):
                if strategy is Strategy.LONG:
                    continue
                self.submit_cycle(self._scheduled_request(strategy, scheduled.scheduled_at, "close_fallback"))
            self._submit_settlement(scheduled.scheduled_at)
        if scheduled.task in _SCORING_INPUT_TASKS and outcome.changed:
            self._trigger_scoring_after_input()

    def _trigger_scoring_after_input(self) -> None:
        completed_at = shanghai_now(self._dependencies.clock.now())
        is_trading_day = self._dependencies.calendar.is_trading_day(completed_at.date())
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
            at=shanghai_now(self._dependencies.clock.now()),
        )

    def _selected_overlay_codes(self) -> tuple[str, ...]:
        codes: list[str] = []
        for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25):
            snapshot = self._dependencies.index.snapshot(strategy)
            identity = snapshot.formal.decision if snapshot.formal is not None else snapshot.current
            if isinstance(identity, ScoredDecision):
                codes.extend(identity_codes(identity))
        return tuple(dict.fromkeys(codes))

    def _refresh_selected_overlays(self, scheduled: ScheduledPipelineTask) -> None:
        for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25):
            snapshot = self._dependencies.index.snapshot(strategy)
            if (
                not isinstance(snapshot.current, ScoredDecision)
                or snapshot.current.trade_date != scheduled.scheduled_at.date()
            ):
                continue
            request = self._scheduled_request(strategy, scheduled.scheduled_at, "quote_overlay")
            for outcome in self._overlay_refresher.refresh(request):
                if outcome.status == "failed":
                    self._record_failure("overlay", outcome.error_code, outcome.strategy)
                elif outcome.status != "skipped":
                    self._record_overlay_success(outcome.strategy, published=outcome.status == "published")
            break

    def wait_idle(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        for task in _DATA_LANES:
            if not self._task_lanes[task].wait_idle(max(0.0, deadline - time.monotonic())):
                return False
        for strategy in Strategy:
            if not self._lanes[strategy].wait_idle(max(0.0, deadline - time.monotonic())):
                return False
        for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25):
            if not self._hybrid_lanes[strategy].wait_idle(max(0.0, deadline - time.monotonic())):
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
        for strategy_lane in self._lanes.values():
            strategy_lane.close()
        for hybrid_lane in self._hybrid_lanes.values():
            hybrid_lane.close()
        steps: list[ShutdownStep] = [self._control.stop(wait=True, cancel_futures=False, deadline=deadline)]
        steps.extend(self._task_lanes[task].stop(deadline=deadline) for task in _DATA_LANES)
        steps.extend(self._lanes[strategy].stop(deadline=deadline) for strategy in Strategy)
        steps.extend(
            self._hybrid_lanes[strategy].stop(deadline=deadline)
            for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)
        )
        steps.append(self._dependencies.observer.stop(deadline=deadline))
        steps.append(self._research.stop(wait=True, deadline=deadline))
        report = ShutdownReport.from_steps(deadline, steps, forced=deadline.expired)
        with self._lock:
            self._shutdown_report = report
        return report

    def status(self) -> V2RuntimeStatus:
        control = self._control.status()
        with self._lock:
            issues = self._issues.snapshot()
            return V2RuntimeStatus(
                running=self._running,
                phase=self._phase,
                config_version=self._config_version,
                lanes=tuple(self._lanes[strategy].status() for strategy in Strategy),
                hybrid_lanes=tuple(
                    self._hybrid_lanes[strategy].status()
                    for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)
                ),
                task_lanes=tuple(self._task_lanes[task].status() for task in _DATA_LANES),
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
            )

    def _scheduled_request(self, strategy: Strategy, observed_at: datetime, phase: str) -> V2CycleRequest:
        current = self._dependencies.index.snapshot(strategy).current
        current_sequence = current.sequence if current is not None else 0
        with self._lock:
            sequence = self._sequences[strategy] + 1
            while sequence <= current_sequence:
                sequence += 2
            self._sequences[strategy] = sequence + 1
        deadline_time = wall_time(11, 18) if strategy is Strategy.TODAY else wall_time(14, 48)
        review_deadline = datetime.combine(observed_at.date(), deadline_time, tzinfo=SHANGHAI)
        allow_review = strategy is not Strategy.LONG and phase != "midday_recovery" and observed_at < review_deadline
        return V2CycleRequest(
            strategy=strategy,
            trade_date=observed_at.date(),
            observed_at=observed_at,
            phase=phase,
            sequence=sequence,
            input_version=f"schedule:{strategy.value}:{observed_at:%Y%m%dT%H%M%S%f}",
            allow_review=allow_review,
            review_deadline=review_deadline,
        )

    def _process_cycle(self, request: V2CycleRequest) -> None:
        lane = self._lanes[request.strategy]
        if lane.is_superseded(request) or self._complete_existing_close_fallback(request):
            return
        local = self._build_fresh_local(request, lane)
        if local is None or not self._publish_fresh_local(request, local, lane):
            return
        self._continue_after_local_publish(request, local, lane)

    def _complete_existing_close_fallback(self, request: V2CycleRequest) -> bool:
        if request.phase == "close_fallback" and request.strategy in {Strategy.TOMORROW, Strategy.D25}:
            snapshot = self._dependencies.index.snapshot(request.strategy)
            if snapshot.formal is not None and snapshot.formal.trade_date == request.trade_date:
                return True
            if isinstance(snapshot.current, ScoredDecision) and snapshot.current.trade_date == request.trade_date:
                self._freeze_close_fallback(request, snapshot.current, recovery_path="current")
                return True
        return False

    def _build_fresh_local(
        self,
        request: V2CycleRequest,
        lane: LatestWinsWorker[V2CycleRequest],
    ) -> DecisionIdentity | None:
        started_at = time.perf_counter()
        if not self._prepare_cycle_data(request):
            return None
        self._record_latency("scoring_data_prepare", started_at)
        if lane.is_superseded(request):
            return None
        started_at = time.perf_counter()
        local = self._build_local(request)
        self._record_latency("local_scoring", started_at)
        if local is None or lane.is_superseded(request):
            return None
        return local

    def _publish_fresh_local(
        self,
        request: V2CycleRequest,
        local: DecisionIdentity,
        lane: LatestWinsWorker[V2CycleRequest],
    ) -> bool:
        if lane.is_superseded(request):
            return False
        started_at = time.perf_counter()
        if not lane.execute_if_current(request, lambda: self._publish(local, hybrid=False)):
            return False
        self._record_latency("decision_publish", started_at)
        return True

    def _continue_after_local_publish(
        self,
        request: V2CycleRequest,
        local: DecisionIdentity,
        lane: LatestWinsWorker[V2CycleRequest],
    ) -> None:
        if lane.is_superseded(request) or not isinstance(local, ScoredDecision):
            return
        defer_initial_review = self._observe_research(local, request)
        if request.phase == "close_fallback":
            self._freeze_close_fallback(request, local, recovery_path="close_rebuild")
            return
        review_now = shanghai_now(self._dependencies.clock.now())
        if request.allow_review and not defer_initial_review and review_now < request.review_deadline:
            self._hybrid_lanes[request.strategy].offer(_HybridUpgradeRequest(local, request))

    def _observe_research(self, local: ScoredDecision, request: V2CycleRequest) -> bool:
        try:
            return self._research.observe(
                self._dependencies.decisions.research_intent(local),
                request,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            self._record_failure("research", _failure_code(exc, "research_intent_failed"), request.strategy)
            return False

    def _process_hybrid(self, request: _HybridUpgradeRequest) -> None:
        if self._hybrid_lanes[request.cycle.strategy].is_superseded(request):
            return
        if shanghai_now(self._dependencies.clock.now()) >= request.cycle.review_deadline:
            return
        self._upgrade_hybrid(request.local, request.cycle)

    def _record_latency(self, stage: str, started_at: float) -> None:
        self._dependencies.latency.record_duration(stage, (time.perf_counter() - started_at) * 1000.0)

    def _on_research_result(self, result: ResearchRefreshResult, initial_rescore: bool) -> None:
        del initial_rescore
        if not result.changed_codes:
            return
        with self._lock:
            if not self._running:
                return
        completed_at = shanghai_now(result.completed_at or self._dependencies.clock.now())
        is_trading_day = self._dependencies.calendar.is_trading_day(completed_at.date())
        schedule = decision_at(completed_at, is_trading_day=is_trading_day)
        if not schedule.should_score:
            return
        strategies: tuple[Strategy, ...] = (Strategy.TOMORROW, Strategy.D25)
        if schedule.phase in {MarketPhase.TODAY_OBSERVE, MarketPhase.TODAY_MAIN, MarketPhase.TODAY_LATE}:
            strategies = (*strategies, Strategy.TODAY)
        risk_version = _research_input_version(result)
        for strategy in strategies:
            current = self._dependencies.index.snapshot(strategy).current
            if not isinstance(current, ScoredDecision) or current.trade_date != completed_at.date():
                continue
            request = self._scheduled_request(strategy, completed_at, schedule.phase.value)
            self.submit_cycle(replace(request, input_version=f"risk:{strategy.value}:{risk_version}"))

    def _refresh_data(self, request: V2CycleRequest) -> bool:
        try:
            self._dependencies.data.refresh(request)
        except V2DataRefreshUnavailableError as exc:
            self._record_failure("refresh", _failure_code(exc, "refresh_unavailable"), request.strategy)
            return False
        return True

    def _prepare_cycle_data(self, request: V2CycleRequest) -> bool:
        if not self._refresh_data(request):
            return False
        for outcome in self._overlay_refresher.refresh(request):
            if outcome.status == "failed":
                self._record_failure("overlay", outcome.error_code, outcome.strategy)
            elif outcome.status != "skipped":
                self._record_overlay_success(outcome.strategy, published=outcome.status == "published")
        if request.phase == "midday_recovery" and request.strategy is Strategy.LONG:
            with self._lock:
                self._midday_long_handoff_date = request.trade_date
        return True

    def _build_local(self, request: V2CycleRequest) -> DecisionIdentity | None:
        try:
            local = self._dependencies.decisions.build_local(request)
            if local is not None:
                _validate_cycle_identity(request, local)
        except V2DecisionUnavailableError as exc:
            self._record_failure("decision", _failure_code(exc, "decision_unavailable"), request.strategy)
            return None
        return local

    def _publish(self, identity: DecisionIdentity, *, hybrid: bool) -> bool:
        expected = self._dependencies.index.snapshot(identity.strategy).current
        if isinstance(identity, ScoredDecision):
            try:
                overlay = self._dependencies.decisions.initial_overlay(identity)
            except V2DecisionUnavailableError as exc:
                self._record_failure("decision", _failure_code(exc, "decision_quote_unavailable"), identity.strategy)
                return False
            published = self._dependencies.index.publish_scored(
                identity,
                overlay,
                expected_version=expected.version if expected is not None else None,
            )
        else:
            published = self._dependencies.index.publish(
                identity,
                expected_version=expected.version if expected is not None else None,
            )
        if not published.accepted:
            self._record_publish_rejection(published.reason, identity.strategy)
            return False
        self._record_publish(hybrid=hybrid, event=published.event, strategy=identity.strategy)
        return True

    def _upgrade_hybrid(self, local: ScoredDecision, request: V2CycleRequest) -> None:
        try:
            hybrid = self._dependencies.reviews.build_hybrid(local, request)
        except V2ReviewUnavailableError as exc:
            self._record_failure("review", _failure_code(exc, "review_unavailable"), request.strategy)
            return
        if hybrid is None:
            return
        try:
            _validate_cycle_identity(request, hybrid)
        except V2DecisionUnavailableError:
            self._record_failure("review", "review_identity_mismatch", request.strategy)
            return
        upgrade = _HybridUpgradeRequest(local, request)
        self._hybrid_lanes[request.strategy].execute_if_current(
            upgrade,
            lambda: self._publish(hybrid, hybrid=True),
        )

    def _record_publish(
        self,
        *,
        hybrid: bool,
        event: V2DecisionCommitted | None,
        strategy: Strategy,
    ) -> None:
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
                self._dependencies.publish_decision(event)
            except (RuntimeError, TypeError, ValueError) as exc:
                self._record_failure("publish", f"decision_event:{type(exc).__name__}", strategy)
        observation = None
        if event is not None:
            try:
                audit = self._dependencies.decisions.research_audit(event.decision_version)
            except (RuntimeError, TypeError, ValueError) as exc:
                self._record_failure("observer", f"research_audit:{type(exc).__name__}", strategy)
                audit = None
            try:
                profile_input = self._dependencies.decisions.tomorrow_profile_research_input(event.decision_version)
            except (RuntimeError, TypeError, ValueError) as exc:
                self._record_failure("observer", f"profile_comparison_input:{type(exc).__name__}", strategy)
                profile_input = None
            observation = V2DecisionObservation(event, audit, profile_input)
        if observation is not None and not self._dependencies.observer.offer(observation):
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
            if decision.phase in {MarketPhase.TODAY_OBSERVE, MarketPhase.TODAY_MAIN, MarketPhase.TODAY_LATE}:
                strategies = (*strategies, Strategy.TODAY)
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

    def _freeze_close_fallback(
        self,
        request: V2CycleRequest,
        current: ScoredDecision,
        *,
        recovery_path: Literal["current", "close_rebuild"],
    ) -> None:
        native_version = dict(current.input_versions).get("native", current.content_hash)
        try:
            self._dependencies.freezes.freeze_close_fallback(
                request.strategy,
                request.observed_at,
                current,
                recovery_path=recovery_path,
                official_close_version=f"official-close:{native_version}",
            )
        except V2FreezeUnavailableError:
            self._record_failure("freeze", "close_fallback_unavailable", request.strategy)
        except Exception as exc:
            self._record_failure("freeze", f"close_fallback_unexpected:{type(exc).__name__}", request.strategy)
        else:
            with self._lock:
                self._freeze_completed_count += 1
                self._resolve_issues_locked(strategy=request.strategy, stages=frozenset({"freeze"}))

    def _submit_freeze(
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
            self._record_failure("freeze", "freeze_capacity_rejected", strategy)
            return False
        return True

    def _submit_checkpoint(
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
            self._record_failure("checkpoint", "checkpoint_capacity_rejected", strategy)
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
        except V2FreezeUnavailableError as exc:
            self._record_failure("checkpoint", _failure_code(exc, "checkpoint_unavailable"), strategy)
        except Exception as exc:
            self._record_failure("checkpoint", f"checkpoint_unexpected:{type(exc).__name__}", strategy)
        else:
            success = True
            with self._lock:
                self._resolve_issues_locked(strategy=strategy, stages=frozenset({"checkpoint"}))
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
        except V2FreezeUnavailableError:
            self._record_failure("freeze", "freeze_unavailable", strategy)
        except Exception as exc:
            self._record_failure("freeze", f"freeze_unexpected:{type(exc).__name__}", strategy)
        else:
            success = True
            with self._lock:
                self._freeze_completed_count += 1
                self._resolve_issues_locked(strategy=strategy, stages=frozenset({"freeze"}))
        finally:
            self._finish_control(key, success=success)
            self._dependencies.cadence.record_point_result(
                at.date().isoformat(),
                scheduled.schedule_point or SchedulePoint.AFTERNOON_FREEZE,
                strategy.value,
                SchedulePointResult.COMPLETED if success else SchedulePointResult.RETRY,
                at=shanghai_now(self._dependencies.clock.now()),
            )

    def _submit_settlement(self, at: datetime) -> None:
        key = f"settlement:{at.date().isoformat()}"
        if not self._reserve_control(key):
            return
        future = self._control.submit(self._run_settlement, key, at)
        if future is None:
            self._finish_control(key, success=False)
            self._record_failure("settlement", "settlement_capacity_rejected")

    def _run_settlement(self, key: str, at: datetime) -> None:
        success = False
        try:
            self._dependencies.settlement.settle(at)
        except V2SettlementUnavailableError:
            self._record_failure("settlement", "settlement_unavailable")
        except Exception as exc:
            self._record_failure("settlement", f"settlement_unexpected:{type(exc).__name__}")
        else:
            success = True
            with self._lock:
                self._settlement_completed_count += 1
                self._resolve_issues_locked(stages=frozenset({"settlement"}))
        finally:
            self._finish_control(key, success=success)

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

    def _record_failure(self, stage: str, code: str, strategy: Strategy | None = None) -> None:
        occurred_at = shanghai_now(self._dependencies.clock.now())
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
            self._issues.record(qualified, stage, strategy, occurred_at)

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
    ) -> None:
        self._issues.resolve(
            shanghai_now(self._dependencies.clock.now()),
            strategy=strategy,
            stages=stages,
        )


def _cycle_phase(strategy: Strategy, phase: MarketPhase) -> str:
    if strategy in {Strategy.TOMORROW, Strategy.D25} and phase is MarketPhase.AFTER_CLOSE:
        return "close_fallback"
    if phase is MarketPhase.MIDDAY:
        return "midday_recovery"
    return cast(str, phase.value)


def _validate_cycle_identity(request: V2CycleRequest, identity: DecisionIdentity) -> None:
    if identity.strategy is not request.strategy or identity.trade_date != request.trade_date:
        raise V2DecisionUnavailableError("decision identity does not match its scheduled cycle")
    if identity.observed_at < request.observed_at:
        raise V2DecisionUnavailableError("decision identity predates its scheduled cycle")


def _cycle_order_key(request: V2CycleRequest) -> int:
    return request.trade_date.toordinal() * 1_000_000_000 + request.sequence


def _cycle_correlation_id(request: V2CycleRequest) -> str:
    return f"score:{request.strategy.value}:{request.trade_date.isoformat()}:{request.sequence}"


def _hybrid_order_key(request: _HybridUpgradeRequest) -> int:
    return _cycle_order_key(request.cycle)


def _pipeline_task_order_key(request: ScheduledPipelineTask) -> int:
    return int(request.scheduled_at.timestamp() * 1_000_000)


def _pipeline_task_correlation_id(request: ScheduledPipelineTask) -> str:
    return f"data:{request.task.value}:{request.scheduled_at.isoformat()}"


def _pipeline_lane(task: PipelineTask) -> PipelineTask:
    if task in {PipelineTask.CURRENT_QUOTES, PipelineTask.CLOSE_QUOTES}:
        return PipelineTask.FULL_MARKET
    if task is PipelineTask.FINAL_CANDIDATE_QUOTES:
        return PipelineTask.CANDIDATE_QUOTES
    return task


def _failure_code(exc: BaseException, fallback: str) -> str:
    value = str(exc).strip().lower().replace(" ", "_")
    if re.fullmatch(r"[a-z0-9_]{1,64}", value) is not None:
        return value
    return fallback


def _research_input_version(result: ResearchRefreshResult) -> str:
    import hashlib

    material = (result.data_version, result.requested_codes, result.changed_codes, result.completed_at)
    return hashlib.sha256(repr(material).encode("utf-8")).hexdigest()[:20]


__all__ = ["V2RuntimeDependencies", "V2RuntimeIssue", "V2RuntimeStatus", "V2SchedulerRuntime"]
