from __future__ import annotations

import threading
from dataclasses import replace
from datetime import date, datetime, timedelta

from tests.unit.domain.test_decision_identity import NOW, decision
from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.decision_observers import AsyncDecisionObserver
from trader.application.ports.v2_runtime import (
    SharedDeepSeekRuntimeContract,
    V2CycleRequest,
    V2DataRefreshUnavailableError,
)
from trader.application.schedule import SHANGHAI, MarketPhase
from trader.application.shutdown import ShutdownDeadline
from trader.application.v2_runtime import V2RuntimeDependencies, V2SchedulerRuntime
from trader.bootstrap import _runtime_status
from trader.domain.recommendation.decision_identity import (
    CommittedDecisionRecord,
    LongProjection,
    LongProjectionItem,
    ScoredDecision,
)
from trader.domain.recommendation.models import Strategy


class TradingCalendar:
    def is_trading_day(self, _day: date) -> bool:
        return True


class FixedClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


class DataRefresh:
    def __init__(self) -> None:
        self.calls: list[Strategy] = []

    def refresh(self, request: V2CycleRequest) -> None:
        self.calls.append(request.strategy)


class Decisions:
    def build_local(self, request: V2CycleRequest):
        if request.strategy is Strategy.LONG:
            return LongProjection(
                request.trade_date,
                request.sequence,
                request.observed_at,
                (("quotes", request.input_version),),
                (LongProjectionItem("600001", "core", request.input_version),),
            )
        return replace(
            decision(request.strategy, sequence=request.sequence),
            trade_date=request.trade_date,
            observed_at=request.observed_at,
        )

    def research_audit(self, version: str):
        del version
        return None


class SharedReviews:
    runtime_contract = SharedDeepSeekRuntimeContract(
        daily_physical_limit=168,
        shared_cache=True,
        shared_single_flight=True,
    )

    def __init__(self) -> None:
        self.calls: list[Strategy] = []

    def build_hybrid(self, local: ScoredDecision, request: V2CycleRequest) -> ScoredDecision:
        self.calls.append(request.strategy)
        return replace(
            local,
            sequence=local.sequence + 1,
            stage="hybrid",
            parent_version=local.version,
            items=(replace(local.items[0], final_score=89.0),),
        )


class Freezes:
    def __init__(self, index: UnifiedDecisionIndex | None = None) -> None:
        self._index = index
        self.calls: list[tuple[Strategy, str | None]] = []
        self.close_fallback_calls: list[tuple[Strategy, str, str]] = []

    def freeze(self, strategy: Strategy, at: datetime, current) -> None:
        self.calls.append((strategy, current.version if current is not None else None))
        if self._index is None:
            return
        sealed = self._index.seal_for_freeze(strategy, boundary_at=at)
        assert sealed.accepted and sealed.decision is not None
        assert self._index.commit_formal(CommittedDecisionRecord(sealed.decision, at, "scheduled"))

    def freeze_close_fallback(
        self,
        strategy: Strategy,
        _at: datetime,
        current,
        *,
        recovery_path: str,
        official_close_version: str,
    ) -> None:
        self.close_fallback_calls.append((strategy, recovery_path, official_close_version))


class Settlement:
    def __init__(self) -> None:
        self.calls: list[datetime] = []

    def settle(self, at: datetime) -> None:
        self.calls.append(at)


def test_v2_fixture_runs_without_the_legacy_pipeline_through_shutdown() -> None:
    morning = datetime(2026, 8, 11, 10, 0, tzinfo=SHANGHAI)
    freeze_at = datetime(2026, 8, 11, 14, 50, tzinfo=SHANGHAI)
    close_at = datetime(2026, 8, 11, 15, 0, tzinfo=SHANGHAI)
    data = DataRefresh()
    reviews = SharedReviews()
    settlement = Settlement()
    observed: list[str] = []
    observer = AsyncDecisionObserver(
        (lambda observation: observed.append(observation.event.event_id),),
        capacity=16,
        thread_name="test-v2-fixture-observer",
    )
    index = UnifiedDecisionIndex()
    freezes = Freezes(index)
    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=FixedClock(morning),
            calendar=TradingCalendar(),
            data=data,
            decisions=Decisions(),
            reviews=reviews,
            index=index,
            observer=observer,
            freezes=freezes,
            settlement=settlement,
        ),
        config_version="runtime-v2",
    )

    assert runtime.start()
    runtime.submit_due()
    assert runtime.wait_idle(2.0)
    runtime.submit_due(freeze_at)
    runtime.submit_due(freeze_at)
    runtime.submit_due(close_at)
    runtime.submit_due(close_at)
    assert runtime.wait_idle(2.0)
    status = runtime.status()
    report = runtime.stop(ShutdownDeadline.start(2.0))

    assert report.completed
    assert set(data.calls) == {Strategy.TODAY, Strategy.TOMORROW, Strategy.D25, Strategy.LONG}
    assert set(reviews.calls) == {Strategy.TODAY, Strategy.TOMORROW, Strategy.D25}
    assert len(observed) == 6
    assert {strategy for strategy, version in freezes.calls if version} == {Strategy.TOMORROW, Strategy.D25}
    assert settlement.calls == [close_at]
    assert status.freeze_completed_count == 2
    assert status.settlement_completed_count == 1
    assert status.phase is MarketPhase.AFTER_CLOSE
    assert status.config_version == "runtime-v2"
    assert all(index.snapshot(strategy).current is not None for strategy in Strategy)
    assert not any(thread.name.startswith("trader-v2-") for thread in threading.enumerate())
    assert runtime.submit_tick(close_at) is False
    assert runtime.status().control_rejected_count == status.control_rejected_count


def test_after_close_cold_start_recovers_missing_scored_strategies_and_long() -> None:
    after_close = datetime(2026, 8, 11, 15, 5, tzinfo=SHANGHAI)
    data = DataRefresh()
    decisions = Decisions()
    reviews = SharedReviews()
    freezes = Freezes()
    index = UnifiedDecisionIndex()
    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=FixedClock(after_close),
            calendar=TradingCalendar(),
            data=data,
            decisions=decisions,
            reviews=reviews,
            index=index,
            observer=AsyncDecisionObserver((), capacity=4, thread_name="test-v2-after-close-observer"),
            freezes=freezes,
            settlement=Settlement(),
        ),
        config_version="runtime-v2",
    )

    runtime.start()
    runtime.submit_due(after_close)
    assert runtime.wait_idle(2.0)
    runtime.stop(ShutdownDeadline.start(2.0))

    assert set(data.calls) == {Strategy.TOMORROW, Strategy.D25, Strategy.LONG}
    assert reviews.calls == []
    assert {(strategy, recovery_path) for strategy, recovery_path, _version in freezes.close_fallback_calls} == {
        (Strategy.TOMORROW, "close_rebuild"),
        (Strategy.D25, "close_rebuild"),
    }
    assert all(version.startswith("official-close:") for _strategy, _path, version in freezes.close_fallback_calls)
    assert index.snapshot(Strategy.TODAY).current is None
    assert all(index.snapshot(strategy).current is not None for strategy in (Strategy.TOMORROW, Strategy.D25))
    assert index.snapshot(Strategy.LONG).current is not None


def test_after_close_prefers_existing_same_day_current_without_rebuilding() -> None:
    before_close = datetime(2026, 8, 11, 14, 49, 55, tzinfo=SHANGHAI)
    after_close = datetime(2026, 8, 11, 15, 5, tzinfo=SHANGHAI)
    data = DataRefresh()
    freezes = Freezes()
    index = UnifiedDecisionIndex()
    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=FixedClock(after_close),
            calendar=TradingCalendar(),
            data=data,
            decisions=Decisions(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncDecisionObserver((), capacity=4, thread_name="test-v2-after-close-current-observer"),
            freezes=freezes,
            settlement=Settlement(),
        ),
        config_version="runtime-v2",
    )
    for strategy in (Strategy.TOMORROW, Strategy.D25):
        current = replace(
            decision(strategy, sequence=1),
            trade_date=before_close.date(),
            observed_at=before_close,
        )
        assert index.publish(current, expected_version=None).accepted

    runtime.start()
    runtime.submit_due(after_close)
    assert runtime.wait_idle(2.0)
    runtime.stop(ShutdownDeadline.start(2.0))

    assert data.calls == [Strategy.LONG]
    assert {(strategy, recovery_path) for strategy, recovery_path, _version in freezes.close_fallback_calls} == {
        (Strategy.TOMORROW, "current"),
        (Strategy.D25, "current"),
    }


def test_tomorrow_lane_progresses_while_today_lane_is_blocked() -> None:
    today_entered = threading.Event()
    today_release = threading.Event()

    class BlockingDecisions(Decisions):
        def build_local(self, request: V2CycleRequest):
            if request.strategy is Strategy.TODAY:
                today_entered.set()
                today_release.wait(timeout=1.0)
            return super().build_local(request)

    index = UnifiedDecisionIndex()
    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=FixedClock(NOW),
            calendar=TradingCalendar(),
            data=DataRefresh(),
            decisions=BlockingDecisions(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncDecisionObserver((), capacity=1, thread_name="test-v2-reserved-observer"),
            freezes=Freezes(),
            settlement=Settlement(),
        ),
        config_version="runtime-v2",
    )
    runtime.start()
    today = V2CycleRequest(Strategy.TODAY, NOW.date(), NOW, "afternoon", 1, "today-v1", True, NOW)
    tomorrow = replace(today, strategy=Strategy.TOMORROW, input_version="tomorrow-v1")
    runtime.submit_cycle(today)
    assert today_entered.wait(timeout=1.0)
    runtime.submit_cycle(tomorrow)

    try:
        assert _wait_for(lambda: index.snapshot(Strategy.TOMORROW).current is not None)
        assert index.snapshot(Strategy.TODAY).current is None
    finally:
        today_release.set()
        runtime.stop(ShutdownDeadline.start(2.0))


def test_refresh_failure_retains_last_valid_decision_without_cascading_build_failure() -> None:
    class FailingData(DataRefresh):
        fail = True

        def refresh(self, request: V2CycleRequest) -> None:
            super().refresh(request)
            if self.fail:
                raise V2DataRefreshUnavailableError("source unavailable")

    class TrackingDecisions(Decisions):
        def __init__(self) -> None:
            self.calls: list[Strategy] = []

        def build_local(self, request: V2CycleRequest):
            self.calls.append(request.strategy)
            return super().build_local(request)

    index = UnifiedDecisionIndex()
    previous = replace(decision(Strategy.TOMORROW, sequence=1), observed_at=NOW - timedelta(minutes=1))
    assert index.publish(previous, expected_version=None).accepted
    decisions = TrackingDecisions()
    data = FailingData()
    clock = FixedClock(NOW)
    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=clock,
            calendar=TradingCalendar(),
            data=data,
            decisions=decisions,
            reviews=SharedReviews(),
            index=index,
            observer=AsyncDecisionObserver((), capacity=1, thread_name="test-v2-failure-observer"),
            freezes=Freezes(),
            settlement=Settlement(),
        ),
        config_version="runtime-v2",
    )
    request = V2CycleRequest(
        Strategy.TOMORROW,
        NOW.date(),
        NOW,
        "afternoon",
        3,
        "input-v1",
        True,
        NOW + timedelta(minutes=1),
    )
    runtime.start()
    runtime.submit_cycle(request)
    assert runtime.wait_idle(1.0)
    status = runtime.status()

    current = index.snapshot(Strategy.TOMORROW).current
    assert current is previous
    assert decisions.calls == []
    assert status.refresh_failure_count == 1
    assert status.decision_failure_count == 0
    assert status.review_failure_count == 0
    assert status.local_publish_count == 0
    assert status.hybrid_publish_count == 0
    assert status.strategy_error_codes == (("tomorrow", "refresh:source_unavailable"),)
    assert len(status.recent_errors) == 1
    issue = status.recent_errors[0]
    assert issue.code == "refresh:source_unavailable"
    assert issue.severity == "degraded"
    assert issue.strategy is Strategy.TOMORROW
    assert issue.stage == "refresh"
    assert issue.occurred_at == NOW
    assert issue.last_occurred_at == NOW
    assert issue.count == 1
    assert issue.recovery_status == "active"
    assert issue.resolved_at is None

    class StatusReviewer:
        def status(self):
            return {"status": "ready"}

    class StatusBudget:
        def summary(self, _day: str):
            return {"limit": 168, "used": 0, "remaining": 168}

    payload = _runtime_status(runtime, StatusReviewer(), StatusBudget())  # type: ignore[arg-type]
    assert payload["degraded_reasons"] == ["tomorrow:refresh:source_unavailable"]
    assert payload["health"] == {"level": "degraded", "issue_count": 1}
    assert payload["recent_errors"] == [
        {
            "code": "refresh:source_unavailable",
            "severity": "degraded",
            "strategy": "tomorrow",
            "stage": "refresh",
            "occurred_at": NOW.isoformat(),
            "last_occurred_at": NOW.isoformat(),
            "count": 1,
            "recovery_status": "active",
            "resolved_at": None,
        }
    ]
    assert payload["runtime_version"] == "runtime-v2"
    assert payload["scheduler"]["decision_failure_count"] == 0  # type: ignore[index]

    data.fail = False
    clock.current = NOW + timedelta(minutes=1)
    runtime.submit_cycle(replace(request, sequence=5, input_version="input-v2", allow_review=False))
    assert runtime.wait_idle(1.0)
    recovered = runtime.status()
    runtime.stop(ShutdownDeadline.start(1.0))

    assert recovered.strategy_error_codes == ()
    assert recovered.last_error_code == ""
    assert recovered.recent_errors[0].recovery_status == "recovered"
    assert recovered.recent_errors[0].resolved_at == NOW + timedelta(minutes=1)


def test_runtime_error_history_is_bounded_and_repeated_failures_are_coalesced() -> None:
    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=FixedClock(NOW),
            calendar=TradingCalendar(),
            data=DataRefresh(),
            decisions=Decisions(),
            reviews=SharedReviews(),
            index=UnifiedDecisionIndex(),
            observer=AsyncDecisionObserver((), capacity=1, thread_name="test-v2-error-history-observer"),
            freezes=Freezes(),
            settlement=Settlement(),
        ),
        config_version="runtime-v2",
    )

    runtime._record_failure("refresh", "source_unavailable", Strategy.TOMORROW)
    runtime._record_failure("refresh", "source_unavailable", Strategy.TOMORROW)
    assert runtime.status().recent_errors[0].count == 2
    for index in range(25):
        runtime._record_failure("settlement", f"failure_{index}")

    status = runtime.status()
    runtime.stop(ShutdownDeadline.start(1.0))

    assert len(status.recent_errors) == 20
    assert status.recent_errors[0].code == "settlement:failure_24"
    assert all(issue.code != "refresh:source_unavailable" for issue in status.recent_errors)


def test_successful_publish_does_not_recover_an_unrelated_freeze_failure() -> None:
    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=FixedClock(NOW),
            calendar=TradingCalendar(),
            data=DataRefresh(),
            decisions=Decisions(),
            reviews=SharedReviews(),
            index=UnifiedDecisionIndex(),
            observer=AsyncDecisionObserver((), capacity=1, thread_name="test-v2-freeze-issue-observer"),
            freezes=Freezes(),
            settlement=Settlement(),
        ),
        config_version="runtime-v2",
    )
    runtime._record_failure("freeze", "freeze_unavailable", Strategy.TOMORROW)
    request = V2CycleRequest(
        Strategy.TOMORROW,
        NOW.date(),
        NOW,
        "afternoon",
        1,
        "input-v1",
        False,
        NOW + timedelta(minutes=1),
    )

    runtime.start()
    runtime.submit_cycle(request)
    assert runtime.wait_idle(1.0)
    status = runtime.status()
    runtime.stop(ShutdownDeadline.start(1.0))

    freeze_issue = next(issue for issue in status.recent_errors if issue.stage == "freeze")
    assert freeze_issue.recovery_status == "active"
    assert status.strategy_error_codes == (("tomorrow", "freeze:freeze_unavailable"),)


def test_review_deadline_prevents_a_late_model_upgrade() -> None:
    reviews = SharedReviews()
    index = UnifiedDecisionIndex()
    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=FixedClock(NOW),
            calendar=TradingCalendar(),
            data=DataRefresh(),
            decisions=Decisions(),
            reviews=reviews,
            index=index,
            observer=AsyncDecisionObserver((), capacity=1, thread_name="test-v2-deadline-observer"),
            freezes=Freezes(),
            settlement=Settlement(),
        ),
        config_version="runtime-v2",
    )
    request = V2CycleRequest(Strategy.TOMORROW, NOW.date(), NOW, "afternoon", 1, "input-v1", True, NOW)
    runtime.start()
    runtime.submit_cycle(request)
    assert runtime.wait_idle(1.0)
    status = runtime.status()
    runtime.stop(ShutdownDeadline.start(1.0))

    current = index.snapshot(Strategy.TOMORROW).current
    assert isinstance(current, ScoredDecision)
    assert current.stage == "local"
    assert reviews.calls == []
    assert status.local_publish_count == 1
    assert status.hybrid_publish_count == 0


def _wait_for(predicate, timeout: float = 1.0) -> bool:
    deadline = threading.Event()
    for _attempt in range(int(timeout / 0.01)):
        if predicate():
            return True
        deadline.wait(0.01)
    return predicate()
