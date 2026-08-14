"""Independent V2 scheduling, strategy lanes, publication, and shutdown."""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime
from datetime import time as wall_time
from typing import Literal, cast

from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.decision_events import V2DecisionCommitted
from trader.application.decision_observers import DecisionObserverRuntime, DecisionObserverStatus
from trader.application.ports.clock import Clock
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
    V2ReviewUnavailableError,
    V2SettlementPort,
    V2SettlementUnavailableError,
    V2TradingCalendarPort,
)
from trader.application.research_audit import V2DecisionObservation
from trader.application.schedule import (
    SHANGHAI,
    MarketPhase,
    SchedulePoint,
    decision_at,
    schedule_point_at,
    seconds_until_next_schedule_boundary,
    shanghai_now,
)
from trader.application.shutdown import ShutdownDeadline, ShutdownReport, ShutdownStep
from trader.application.v2_lifecycle import LatestWinsOffer, LatestWinsStatus, LatestWinsWorker
from trader.application.workers import BoundedExecutor
from trader.domain.recommendation.decision_identity import DecisionIdentity, LongProjection, ScoredDecision
from trader.domain.recommendation.models import Strategy

_ISSUE_HISTORY_CAPACITY = 20


@dataclass(frozen=True)
class V2RuntimeDependencies:
    clock: Clock
    calendar: V2TradingCalendarPort
    data: V2DataRefreshPort
    decisions: V2DecisionBuilderPort
    reviews: V2DeepSeekUpgradePort
    index: UnifiedDecisionIndex
    observer: DecisionObserverRuntime
    freezes: V2FreezePort
    settlement: V2SettlementPort


@dataclass(frozen=True)
class V2RuntimeIssue:
    code: str
    severity: Literal["degraded", "error"]
    strategy: Strategy | None
    stage: str
    occurred_at: datetime
    last_occurred_at: datetime
    count: int
    recovery_status: Literal["active", "recovered"]
    resolved_at: datetime | None


@dataclass(frozen=True)
class V2RuntimeStatus:
    running: bool
    phase: MarketPhase
    config_version: str
    lanes: tuple[LatestWinsStatus, ...]
    observer: DecisionObserverStatus
    deepseek: SharedDeepSeekRuntimeContract
    control_running: bool
    control_inflight: int
    control_rejected_count: int
    refresh_failure_count: int
    decision_failure_count: int
    review_failure_count: int
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
            )
            for strategy in Strategy
        }
        self._refresh_failure_count = 0
        self._decision_failure_count = 0
        self._review_failure_count = 0
        self._local_publish_count = 0
        self._hybrid_publish_count = 0
        self._publish_rejection_count = 0
        self._observer_rejection_count = 0
        self._freeze_completed_count = 0
        self._freeze_failure_count = 0
        self._settlement_completed_count = 0
        self._settlement_failure_count = 0
        self._last_error_code = ""
        self._system_error_code = ""
        self._strategy_error_codes: dict[Strategy, str] = {}
        self._recent_errors: OrderedDict[tuple[str, str], V2RuntimeIssue] = OrderedDict()

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
        for strategy in Strategy:
            if not self._lanes[strategy].start():
                raise RuntimeError(f"V2 {strategy.value} lane did not start")

    def _abort_start(self) -> None:
        deadline = ShutdownDeadline.start(self._shutdown_timeout_seconds)
        for lane in self._lanes.values():
            lane.close()
        for lane in self._lanes.values():
            lane.stop(deadline=deadline)
        self._control.stop(wait=True, cancel_futures=True, deadline=deadline)
        self._dependencies.observer.stop(deadline=deadline)
        with self._lock:
            self._running = False
            self._stopped = True

    def submit_due(self, at: datetime | None = None) -> float:
        with self._lock:
            if not self._running:
                return 30.0
        observed_at = shanghai_now(at or self._dependencies.clock.now())
        is_trading_day = self._dependencies.calendar.is_trading_day(observed_at.date())
        decision = decision_at(observed_at, is_trading_day=is_trading_day)
        with self._lock:
            self._phase = decision.phase
        schedule_point = schedule_point_at(observed_at, is_trading_day=is_trading_day)
        strategies: tuple[Strategy, ...] = ()
        if decision.phase is MarketPhase.AFTER_CLOSE and schedule_point is not SchedulePoint.CLOSE_QUOTES:
            strategies = self._after_close_recovery_strategies(observed_at)
        elif decision.should_score:
            strategies = (Strategy.TOMORROW, Strategy.D25)
            if decision.phase in {MarketPhase.TODAY_OBSERVE, MarketPhase.TODAY_MAIN, MarketPhase.TODAY_LATE}:
                strategies = (*strategies, Strategy.TODAY)
        if decision.phase is not MarketPhase.AFTER_CLOSE and decision.should_refresh_market:
            strategies = (*strategies, Strategy.LONG)
        for strategy in strategies:
            phase = (
                "close_fallback"
                if strategy in {Strategy.TOMORROW, Strategy.D25} and decision.phase is MarketPhase.AFTER_CLOSE
                else decision.phase.value
            )
            self.submit_cycle(self._scheduled_request(strategy, observed_at, phase))
        for raw_strategy in decision.freeze_strategies:
            self._submit_freeze(Strategy(raw_strategy), observed_at)
        if schedule_point is SchedulePoint.CLOSE_QUOTES:
            self._submit_settlement(observed_at)
        return seconds_until_next_schedule_boundary(observed_at, maximum_seconds=30.0)

    def submit_tick(self, at: datetime | None = None) -> bool:
        with self._lock:
            if not self._running:
                return False
        self.submit_due(at)
        return True

    def submit_cycle(self, request: V2CycleRequest) -> LatestWinsOffer:
        with self._lock:
            if not self._running:
                return LatestWinsOffer.REJECTED
        return self._lanes[request.strategy].offer(request)

    def wait_idle(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        for strategy in Strategy:
            if not self._lanes[strategy].wait_idle(max(0.0, deadline - time.monotonic())):
                return False
        while cast(int, self._control.status()["inflight"]) > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return False
            threading.Event().wait(min(0.01, remaining))
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
        self._dependencies.observer.close()
        for lane in self._lanes.values():
            lane.close()
        steps: list[ShutdownStep] = [self._control.stop(wait=True, cancel_futures=False, deadline=deadline)]
        steps.extend(self._lanes[strategy].stop(deadline=deadline) for strategy in Strategy)
        steps.append(self._dependencies.observer.stop(deadline=deadline))
        report = ShutdownReport.from_steps(deadline, steps, forced=deadline.expired)
        with self._lock:
            self._shutdown_report = report
        return report

    def status(self) -> V2RuntimeStatus:
        control = self._control.status()
        with self._lock:
            return V2RuntimeStatus(
                running=self._running,
                phase=self._phase,
                config_version=self._config_version,
                lanes=tuple(self._lanes[strategy].status() for strategy in Strategy),
                observer=self._dependencies.observer.status(),
                deepseek=self._dependencies.reviews.runtime_contract,
                control_running=bool(control["running"]),
                control_inflight=cast(int, control["inflight"]),
                control_rejected_count=cast(int, control["rejected_count"]),
                refresh_failure_count=self._refresh_failure_count,
                decision_failure_count=self._decision_failure_count,
                review_failure_count=self._review_failure_count,
                local_publish_count=self._local_publish_count,
                hybrid_publish_count=self._hybrid_publish_count,
                publish_rejection_count=self._publish_rejection_count,
                observer_rejection_count=self._observer_rejection_count,
                freeze_completed_count=self._freeze_completed_count,
                freeze_failure_count=self._freeze_failure_count,
                settlement_completed_count=self._settlement_completed_count,
                settlement_failure_count=self._settlement_failure_count,
                last_error_code=self._last_error_code,
                strategy_error_codes=tuple(
                    (strategy.value, self._strategy_error_codes[strategy])
                    for strategy in Strategy
                    if strategy in self._strategy_error_codes
                ),
                recent_errors=self._sorted_recent_errors_locked(),
            )

    def _scheduled_request(self, strategy: Strategy, observed_at: datetime, phase: str) -> V2CycleRequest:
        with self._lock:
            sequence = self._sequences[strategy] + 1
            self._sequences[strategy] += 2
        deadline_time = wall_time(11, 18) if strategy is Strategy.TODAY else wall_time(14, 48)
        review_deadline = datetime.combine(observed_at.date(), deadline_time, tzinfo=SHANGHAI)
        allow_review = strategy is not Strategy.LONG and observed_at < review_deadline
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
        if request.phase == "close_fallback" and request.strategy in {Strategy.TOMORROW, Strategy.D25}:
            snapshot = self._dependencies.index.snapshot(request.strategy)
            if snapshot.formal is not None and snapshot.formal.trade_date == request.trade_date:
                return
            if isinstance(snapshot.current, ScoredDecision) and snapshot.current.trade_date == request.trade_date:
                self._freeze_close_fallback(request, snapshot.current, recovery_path="current")
                return
        if not self._refresh_data(request):
            return
        local = self._build_local(request)
        if local is None or not self._publish(local, hybrid=False):
            return
        if request.phase == "close_fallback" and isinstance(local, ScoredDecision):
            self._freeze_close_fallback(request, local, recovery_path="close_rebuild")
            return
        review_now = shanghai_now(self._dependencies.clock.now())
        if request.allow_review and review_now < request.review_deadline and isinstance(local, ScoredDecision):
            self._upgrade_hybrid(local, request)

    def _refresh_data(self, request: V2CycleRequest) -> bool:
        try:
            self._dependencies.data.refresh(request)
        except V2DataRefreshUnavailableError as exc:
            self._record_failure("refresh", _failure_code(exc, "refresh_unavailable"), request.strategy)
            return False
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
        self._publish(hybrid, hybrid=True)

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
        observation = (
            V2DecisionObservation(event, self._dependencies.decisions.research_audit(event.decision_version))
            if event is not None
            else None
        )
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
        strategies: list[Strategy] = []
        for strategy in (Strategy.TOMORROW, Strategy.D25):
            formal = self._dependencies.index.snapshot(strategy).formal
            if formal is None or formal.trade_date != observed_at.date():
                strategies.append(strategy)
        long_current = self._dependencies.index.snapshot(Strategy.LONG).current
        if not isinstance(long_current, LongProjection) or long_current.trade_date != observed_at.date():
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

    def _submit_freeze(self, strategy: Strategy, at: datetime) -> None:
        key = f"freeze:{at.date().isoformat()}:{strategy.value}"
        if not self._reserve_control(key):
            return
        future = self._control.submit_urgent(self._run_freeze, key, strategy, at)
        if future is None:
            self._finish_control(key, success=False)
            self._record_failure("freeze", "freeze_capacity_rejected", strategy)

    def _run_freeze(self, key: str, strategy: Strategy, at: datetime) -> None:
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
            elif stage == "freeze":
                self._freeze_failure_count += 1
            elif stage == "settlement":
                self._settlement_failure_count += 1
            qualified = f"{stage}:{code}"
            if strategy is None:
                self._system_error_code = qualified
            else:
                self._strategy_error_codes[strategy] = qualified
            self._last_error_code = qualified
            self._record_issue_locked(qualified, stage, strategy, occurred_at)

    def _record_issue_locked(
        self,
        code: str,
        stage: str,
        strategy: Strategy | None,
        occurred_at: datetime,
    ) -> None:
        key = (strategy.value if strategy is not None else "system", code)
        existing = self._recent_errors.pop(key, None)
        self._recent_errors[key] = V2RuntimeIssue(
            code=code,
            severity="error" if stage in {"freeze", "settlement", "publish"} else "degraded",
            strategy=strategy,
            stage=stage,
            occurred_at=existing.occurred_at if existing is not None else occurred_at,
            last_occurred_at=occurred_at,
            count=existing.count + 1 if existing is not None else 1,
            recovery_status="active",
            resolved_at=None,
        )
        while len(self._recent_errors) > _ISSUE_HISTORY_CAPACITY:
            self._recent_errors.popitem(last=False)

    def _resolve_issues_locked(
        self,
        *,
        strategy: Strategy | None = None,
        stages: frozenset[str] | None = None,
    ) -> None:
        resolved_at = shanghai_now(self._dependencies.clock.now())
        for key, issue in tuple(self._recent_errors.items()):
            if issue.recovery_status != "active":
                continue
            if strategy is not None and issue.strategy is not strategy:
                continue
            if stages is not None and issue.stage not in stages:
                continue
            self._recent_errors[key] = replace(issue, recovery_status="recovered", resolved_at=resolved_at)
        if strategy is None and stages is not None:
            self._system_error_code = self._latest_active_system_code_locked()
        elif strategy is not None:
            active_code = self._latest_active_strategy_code_locked(strategy)
            if active_code:
                self._strategy_error_codes[strategy] = active_code
            else:
                self._strategy_error_codes.pop(strategy, None)
        self._refresh_last_error_code()

    def _latest_active_strategy_code_locked(self, strategy: Strategy) -> str:
        for issue in reversed(self._recent_errors.values()):
            if issue.strategy is strategy and issue.recovery_status == "active":
                return issue.code
        return ""

    def _latest_active_system_code_locked(self) -> str:
        for issue in reversed(self._recent_errors.values()):
            if issue.strategy is None and issue.recovery_status == "active":
                return issue.code
        return ""

    def _sorted_recent_errors_locked(self) -> tuple[V2RuntimeIssue, ...]:
        return tuple(
            sorted(
                reversed(self._recent_errors.values()),
                key=lambda issue: (
                    0 if issue.severity == "error" else 1,
                    0 if issue.recovery_status == "active" else 1,
                    -issue.last_occurred_at.timestamp(),
                ),
            )
        )

    def _refresh_last_error_code(self) -> None:
        if self._system_error_code:
            self._last_error_code = self._system_error_code
            return
        self._last_error_code = next(reversed(self._strategy_error_codes.values()), "")


def _validate_cycle_identity(request: V2CycleRequest, identity: DecisionIdentity) -> None:
    if identity.strategy is not request.strategy or identity.trade_date != request.trade_date:
        raise V2DecisionUnavailableError("decision identity does not match its scheduled cycle")
    if identity.observed_at < request.observed_at:
        raise V2DecisionUnavailableError("decision identity predates its scheduled cycle")


def _cycle_order_key(request: V2CycleRequest) -> int:
    return request.trade_date.toordinal() * 1_000_000_000 + request.sequence


def _failure_code(exc: BaseException, fallback: str) -> str:
    value = str(exc).strip().lower().replace(" ", "_")
    if re.fullmatch(r"[a-z0-9_]{1,64}", value) is not None:
        return value
    return fallback


__all__ = ["V2RuntimeDependencies", "V2RuntimeIssue", "V2RuntimeStatus", "V2SchedulerRuntime"]
