from __future__ import annotations

import threading
from dataclasses import replace
from datetime import date, datetime, timedelta

from tests.unit.domain.test_decision_identity import NOW, decision
from trader.application.cadence import CadencePlanner, CadencePolicy, PipelineTask
from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.decision_observers import AsyncDecisionObserver
from trader.application.ports.market import ResearchRefreshResult
from trader.application.ports.v2_runtime import (
    SharedDeepSeekRuntimeContract,
    V2CycleRequest,
    V2DataRefreshUnavailableError,
    V2DecisionUnavailableError,
    V2ResearchIntent,
    V2ResearchRuntimeStatus,
)
from trader.application.schedule import SHANGHAI, MarketPhase
from trader.application.shutdown import ShutdownDeadline, ShutdownStep
from trader.application.v2_runtime import V2RuntimeDependencies, V2SchedulerRuntime
from trader.bootstrap_status import runtime_status
from trader.domain.recommendation.decision_identity import (
    CommittedDecisionRecord,
    DecisionOverlay,
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
        self.requests: list[V2CycleRequest] = []
        self.task_requests: list[object] = []

    def refresh(self, request: V2CycleRequest) -> None:
        self.calls.append(request.strategy)
        self.requests.append(request)

    def refresh_task(self, request) -> None:
        self.task_requests.append(request)


def _cadence(at: datetime) -> CadencePlanner:
    return CadencePlanner(
        CadencePolicy.from_seconds(
            {
                "full_market": {
                    "warmup": 10,
                    "today_main": 10,
                    "today_late": 10,
                    "midday": 10,
                    "afternoon": 10,
                    "final_review": 10,
                },
                "candidate_quotes": {
                    "warmup": 2,
                    "today_main": 1,
                    "today_late": 2,
                    "midday": 10,
                    "afternoon": 2,
                    "final_review": 1,
                    "final_window": 1,
                },
                "topk_quotes": {
                    "warmup": 1,
                    "today_main": 1,
                    "today_late": 1,
                    "midday": 10,
                    "afternoon": 1,
                    "final_review": 1,
                    "final_window": 1,
                    "after_close": 10,
                },
                "intraday_tail": {"afternoon": 5, "final_review": 3},
                "long_quotes": {
                    "warmup": 1,
                    "today_main": 1,
                    "today_late": 1,
                    "midday": 10,
                    "afternoon": 1,
                    "final_review": 1,
                    "final_window": 1,
                },
                "score": {"warmup": 10, "today_main": 3, "today_late": 5, "afternoon": 5, "final_review": 3},
                "industry_heat": {
                    "warmup": 120,
                    "today_main": 60,
                    "today_late": 60,
                    "afternoon": 60,
                    "final_review": 60,
                },
                "market_news": {"warmup": 120, "today_main": 60, "today_late": 60, "afternoon": 60, "final_review": 60},
                "stock_risk": {
                    "warmup": 300,
                    "today_main": 180,
                    "today_late": 180,
                    "afternoon": 180,
                    "final_review": 120,
                },
            }
        ),
        started_at=at,
    )


class Decisions:
    def input_quality_status(self):
        return ()

    def has_local_draft(self, strategy: Strategy, trade_date: date) -> bool:
        del strategy, trade_date
        return False

    def build_local(self, request: V2CycleRequest):
        if request.strategy is Strategy.LONG:
            return LongProjection(
                request.trade_date,
                request.sequence,
                request.observed_at,
                (("quotes", request.input_version),),
                (LongProjectionItem("600001", "core", request.input_version),),
            )
        current = decision(request.strategy, sequence=request.sequence)
        items = tuple(
            replace(
                item,
                quote=replace(item.quote, source_time=request.observed_at) if item.quote is not None else None,
            )
            for item in current.items
        )
        return replace(
            current,
            trade_date=request.trade_date,
            observed_at=request.observed_at,
            items=items,
        )

    def initial_overlay(self, decision: ScoredDecision) -> DecisionOverlay:
        quotes = tuple(item.quote for item in decision.items if item.selected and item.quote is not None)
        return DecisionOverlay(
            decision.strategy,
            decision.trade_date,
            decision.version,
            decision.observed_at,
            quotes,
        )

    def refreshed_overlay(
        self,
        decision: ScoredDecision,
        request: V2CycleRequest,
        previous: DecisionOverlay | None,
    ) -> DecisionOverlay | None:
        del previous
        quotes = tuple(
            replace(
                item.quote,
                price=(item.quote.price or 0.0) + 1.0,
                source_time=request.observed_at,
                data_version=request.input_version,
            )
            for item in decision.items
            if item.selected and item.quote is not None
        )
        if not quotes:
            return None
        return DecisionOverlay(
            decision.strategy,
            decision.trade_date,
            decision.version,
            request.observed_at,
            quotes,
        )

    def research_audit(self, version: str):
        del version
        return None

    def research_intent(self, current: ScoredDecision) -> V2ResearchIntent:
        codes = tuple(item.code for item in current.items)
        return V2ResearchIntent(current.strategy, current.trade_date, codes, codes)


class NoopResearchRuntime:
    def start(self) -> bool:
        return True

    def stop(self, *, wait: bool, deadline=None) -> ShutdownStep:
        del wait, deadline
        return ShutdownStep("research", completed=True, timed_out=False)

    def observe(self, intent, request) -> bool:
        del intent, request
        return False

    def offer_due(self, at, phase, *, is_trading_day) -> bool:
        del at, phase, is_trading_day
        return False

    def wait_until_idle(self, timeout_seconds: float) -> bool:
        del timeout_seconds
        return True

    def status(self):
        return V2ResearchRuntimeStatus(state="idle")


def noop_research_factory(_on_result):
    return NoopResearchRuntime()


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


def test_scheduler_atomically_publishes_complete_quotes_for_local_and_hybrid() -> None:
    index = UnifiedDecisionIndex()
    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=FixedClock(NOW),
            calendar=TradingCalendar(),
            cadence=_cadence(NOW),
            data=DataRefresh(),
            decisions=Decisions(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncDecisionObserver((), capacity=4, thread_name="test-v2-overlay-observer"),
            freezes=Freezes(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_decision=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-v2",
    )
    request = V2CycleRequest(
        Strategy.TOMORROW,
        NOW.date(),
        NOW,
        "afternoon",
        1,
        "input-v1",
        True,
        NOW + timedelta(minutes=1),
    )

    runtime.start()
    runtime.submit_cycle(request)
    assert runtime.wait_idle(2.0)
    runtime.stop(ShutdownDeadline.start(2.0))

    snapshot = index.snapshot(Strategy.TOMORROW)
    assert isinstance(snapshot.current, ScoredDecision)
    assert snapshot.current.stage == "hybrid"
    assert snapshot.overlay is not None
    assert snapshot.overlay.parent_version == snapshot.current.version
    assert snapshot.overlay.quotes == (snapshot.current.items[0].quote,)
    assert snapshot.overlay.quotes[0].amount == 120_000_000.0
    assert snapshot.overlay.quotes[0].turnover_rate == 2.1
    assert snapshot.overlay.quotes[0].market_cap == 12_000_000_000.0


def test_scheduler_refreshes_frozen_today_overlay_without_mutating_formal_decision() -> None:
    observed_at = datetime(2026, 8, 11, 13, 5, tzinfo=SHANGHAI)
    frozen_at = observed_at.replace(hour=11, minute=20)
    index = UnifiedDecisionIndex()
    source = decision(Strategy.TODAY, sequence=1)
    source_quote = source.items[0].quote
    assert source_quote is not None
    source = replace(
        source,
        trade_date=observed_at.date(),
        observed_at=frozen_at - timedelta(seconds=1),
        items=(
            replace(
                source.items[0],
                quote=replace(source_quote, source_time=frozen_at - timedelta(seconds=1)),
            ),
        ),
    )
    record = CommittedDecisionRecord(source, frozen_at, "scheduled")
    assert index.restore_formal(record)
    overlay_events: list[DecisionOverlay] = []
    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=FixedClock(observed_at),
            calendar=TradingCalendar(),
            cadence=_cadence(observed_at),
            data=DataRefresh(),
            decisions=Decisions(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncDecisionObserver((), capacity=4, thread_name="test-v2-frozen-overlay-observer"),
            freezes=Freezes(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_decision=lambda _event: None,
            publish_overlay=overlay_events.append,
        ),
        config_version="runtime-v2",
    )
    request = V2CycleRequest(
        Strategy.TOMORROW,
        observed_at.date(),
        observed_at,
        "afternoon",
        1,
        "quotes-v2",
        False,
        observed_at,
    )

    runtime.start()
    runtime.submit_cycle(request)
    assert runtime.wait_idle(2.0)
    runtime.stop(ShutdownDeadline.start(2.0))

    snapshot = index.snapshot(Strategy.TODAY)
    assert snapshot.formal == record
    assert snapshot.current == record.decision
    assert snapshot.overlay is not None
    assert snapshot.overlay.parent_version == record.decision.version
    assert snapshot.overlay.quotes[0].price == (source_quote.price or 0.0) + 1.0
    assert overlay_events == [snapshot.overlay]


def test_frozen_cadence_targets_formal_codes_and_refreshes_overlay_without_rescoring() -> None:
    frozen_at = datetime(2026, 8, 11, 14, 55, tzinfo=SHANGHAI)
    index = UnifiedDecisionIndex()
    source = decision(Strategy.TODAY, sequence=1)
    anchor = source.items[0].quote
    assert anchor is not None
    source = replace(
        source,
        trade_date=frozen_at.date(),
        observed_at=frozen_at.replace(hour=11, minute=19),
        items=(replace(source.items[0], quote=replace(anchor, source_time=frozen_at.replace(hour=11, minute=19))),),
    )
    record = CommittedDecisionRecord(source, frozen_at.replace(hour=11, minute=20), "scheduled")
    assert index.restore_formal(record)
    data = DataRefresh()
    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=FixedClock(frozen_at),
            calendar=TradingCalendar(),
            cadence=_cadence(frozen_at),
            data=data,
            decisions=Decisions(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncDecisionObserver((), capacity=4, thread_name="test-v2-frozen-cadence-observer"),
            freezes=Freezes(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_decision=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-v2",
    )

    runtime.start()
    runtime.submit_due(frozen_at)
    assert runtime.wait_idle(2.0)
    runtime.stop(ShutdownDeadline.start(2.0))

    topk = next(request for request in data.task_requests if request.task is PipelineTask.TOPK_QUOTES)
    assert topk.selected_codes == ("600001",)
    assert not [
        request
        for request in data.requests
        if request.strategy is not Strategy.LONG and request.phase != "quote_overlay"
    ]
    snapshot = index.snapshot(Strategy.TODAY)
    assert snapshot.formal == record
    assert snapshot.overlay is not None
    assert snapshot.overlay.quotes[0].price == (anchor.price or 0.0) + 1.0


def test_scheduler_recovers_overlay_issue_after_later_success() -> None:
    observed_at = datetime(2026, 8, 11, 13, 5, tzinfo=SHANGHAI)
    clock = FixedClock(observed_at)
    index = UnifiedDecisionIndex()
    source = decision(Strategy.TODAY, sequence=1)
    source_quote = source.items[0].quote
    assert source_quote is not None
    source = replace(
        source,
        trade_date=observed_at.date(),
        observed_at=observed_at - timedelta(minutes=1),
        items=(replace(source.items[0], quote=replace(source_quote, source_time=observed_at - timedelta(minutes=1))),),
    )
    assert index.restore_formal(CommittedDecisionRecord(source, observed_at, "scheduled"))

    class FailingOnceOverlayDecisions(Decisions):
        def __init__(self) -> None:
            self.failed = False

        def refreshed_overlay(self, current, request, previous):
            if current.strategy is Strategy.TODAY and not self.failed:
                self.failed = True
                raise V2DecisionUnavailableError("overlay_source_unavailable")
            return super().refreshed_overlay(current, request, previous)

    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=clock,
            calendar=TradingCalendar(),
            cadence=_cadence(observed_at),
            data=DataRefresh(),
            decisions=FailingOnceOverlayDecisions(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncDecisionObserver((), capacity=4, thread_name="test-v2-overlay-recovery-observer"),
            freezes=Freezes(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_decision=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-v2",
    )
    first = V2CycleRequest(
        Strategy.TOMORROW,
        observed_at.date(),
        observed_at,
        "afternoon",
        1,
        "quotes-v2",
        False,
        observed_at,
    )
    second_at = observed_at + timedelta(seconds=30)
    second = replace(first, observed_at=second_at, input_version="quotes-v3")

    runtime.start()
    runtime.submit_cycle(first)
    assert runtime.wait_idle(2.0)
    failed = runtime.status()
    assert failed.overlay_failure_count == 1
    assert any(issue.stage == "overlay" and issue.recovery_status == "active" for issue in failed.recent_errors)

    clock.current = second_at
    runtime.submit_cycle(second)
    assert runtime.wait_idle(2.0)
    recovered = runtime.status()
    runtime.stop(ShutdownDeadline.start(2.0))

    assert recovered.overlay_publish_count >= 1
    assert any(issue.stage == "overlay" and issue.recovery_status == "recovered" for issue in recovered.recent_errors)
    assert dict(recovered.strategy_error_codes).get("today") is None


def test_scheduler_publishes_local_before_research_and_defers_first_review_until_risk_rescore() -> None:
    index = UnifiedDecisionIndex()
    reviews = SharedReviews()
    holder: list[object] = []

    class RecordingResearchRuntime(NoopResearchRuntime):
        def __init__(self, on_result) -> None:
            self.on_result = on_result
            self.observed_versions: list[str] = []

        def observe(self, intent, request) -> bool:
            del intent
            current = index.snapshot(request.strategy).current
            assert current is not None
            self.observed_versions.append(current.version)
            return len(self.observed_versions) == 1

    def research_factory(on_result):
        research = RecordingResearchRuntime(on_result)
        holder.append(research)
        return research

    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=FixedClock(NOW),
            calendar=TradingCalendar(),
            cadence=_cadence(NOW),
            data=DataRefresh(),
            decisions=Decisions(),
            reviews=reviews,
            index=index,
            observer=AsyncDecisionObserver((), capacity=4, thread_name="test-v2-research-order-observer"),
            freezes=Freezes(),
            settlement=Settlement(),
            research_factory=research_factory,
            publish_decision=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-v2",
    )
    request = V2CycleRequest(
        Strategy.TOMORROW,
        NOW.date(),
        NOW,
        "today_main",
        1,
        "input-v1",
        True,
        NOW + timedelta(minutes=1),
    )

    runtime.start()
    runtime.submit_cycle(request)
    assert runtime.wait_idle(2.0)
    research = holder[0]
    assert isinstance(research, RecordingResearchRuntime)
    assert reviews.calls == []
    assert len(research.observed_versions) == 1

    research.on_result(
        ResearchRefreshResult(
            requested_codes=("600001",),
            completed_codes=("600001",),
            changed_codes=("600001",),
            covered_codes=("600001",),
            data_version="research:changed",
            started_at=NOW,
            completed_at=NOW + timedelta(seconds=1),
        ),
        True,
    )
    assert runtime.wait_idle(2.0)
    runtime.stop(ShutdownDeadline.start(2.0))

    assert len(research.observed_versions) == 2
    assert reviews.calls == [Strategy.TOMORROW]
    current = index.snapshot(Strategy.TOMORROW).current
    assert isinstance(current, ScoredDecision)
    assert current.stage == "hybrid"


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
            cadence=_cadence(morning),
            data=data,
            decisions=Decisions(),
            reviews=reviews,
            index=index,
            observer=observer,
            freezes=freezes,
            settlement=settlement,
            research_factory=noop_research_factory,
            publish_decision=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-v2",
    )

    assert runtime.start()
    runtime.submit_due()
    assert runtime.wait_idle(2.0)
    runtime.submit_due(freeze_at)
    runtime.submit_due(freeze_at)
    assert runtime.wait_idle(2.0)
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
    assert runtime.submit_due(close_at) == 30.0
    assert runtime.status().control_rejected_count == status.control_rejected_count


def test_after_close_cold_start_recovers_missing_scored_strategies_and_long() -> None:
    after_close = datetime(2026, 8, 11, 15, 5, tzinfo=SHANGHAI)
    data = DataRefresh()
    decisions = Decisions()
    reviews = SharedReviews()
    freezes = Freezes()
    settlement = Settlement()
    index = UnifiedDecisionIndex()
    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=FixedClock(after_close),
            calendar=TradingCalendar(),
            cadence=_cadence(after_close),
            data=data,
            decisions=decisions,
            reviews=reviews,
            index=index,
            observer=AsyncDecisionObserver((), capacity=4, thread_name="test-v2-after-close-observer"),
            freezes=freezes,
            settlement=settlement,
            research_factory=noop_research_factory,
            publish_decision=lambda _event: None,
            publish_overlay=lambda _overlay: None,
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
    assert settlement.calls == [after_close]


def test_midday_cold_start_recovers_only_missing_non_today_outputs_once_without_review() -> None:
    midday = datetime(2026, 8, 11, 12, 15, tzinfo=SHANGHAI)
    data = DataRefresh()
    reviews = SharedReviews()
    index = UnifiedDecisionIndex()
    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=FixedClock(midday),
            calendar=TradingCalendar(),
            cadence=_cadence(midday),
            data=data,
            decisions=Decisions(),
            reviews=reviews,
            index=index,
            observer=AsyncDecisionObserver((), capacity=4, thread_name="test-v2-midday-observer"),
            freezes=Freezes(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_decision=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-v2",
    )

    runtime.start()
    runtime.submit_due(midday)
    assert runtime.wait_idle(2.0)
    runtime.submit_due(midday + timedelta(seconds=30))
    assert runtime.wait_idle(2.0)
    runtime.stop(ShutdownDeadline.start(2.0))

    scoring_calls = [request.strategy for request in data.requests if request.phase != "quote_overlay"]
    assert scoring_calls.count(Strategy.TODAY) == 0
    assert {
        strategy: scoring_calls.count(strategy) for strategy in (Strategy.TOMORROW, Strategy.D25, Strategy.LONG)
    } == {
        Strategy.TOMORROW: 1,
        Strategy.D25: 1,
        Strategy.LONG: 2,
    }
    assert {request.phase for request in data.requests} <= {"midday", "midday_recovery", "quote_overlay"}
    assert not any(request.allow_review for request in data.requests if request.phase != "quote_overlay")
    assert reviews.calls == []
    assert index.snapshot(Strategy.TODAY).current is None


def test_midday_empty_observation_draft_is_a_completed_recovery_not_a_retry_loop() -> None:
    midday = datetime(2026, 8, 11, 12, 15, tzinfo=SHANGHAI)

    class DraftOnlyDecisions(Decisions):
        def __init__(self) -> None:
            self.drafts: set[tuple[Strategy, date]] = set()

        def has_local_draft(self, strategy: Strategy, trade_date: date) -> bool:
            return (strategy, trade_date) in self.drafts

        def build_local(self, request: V2CycleRequest):
            if request.strategy is Strategy.TOMORROW:
                self.drafts.add((request.strategy, request.trade_date))
                raise V2DecisionUnavailableError("security_master_coverage_incomplete")
            return super().build_local(request)

    data = DataRefresh()
    decisions = DraftOnlyDecisions()
    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=FixedClock(midday),
            calendar=TradingCalendar(),
            cadence=_cadence(midday),
            data=data,
            decisions=decisions,
            reviews=SharedReviews(),
            index=UnifiedDecisionIndex(),
            observer=AsyncDecisionObserver((), capacity=4, thread_name="test-v2-midday-draft-observer"),
            freezes=Freezes(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_decision=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-v2",
    )

    runtime.start()
    runtime.submit_due(midday)
    assert runtime.wait_idle(2.0)
    runtime.submit_due(midday + timedelta(seconds=30))
    assert runtime.wait_idle(2.0)
    runtime.stop(ShutdownDeadline.start(2.0))

    assert data.calls.count(Strategy.TOMORROW) == 1
    assert decisions.has_local_draft(Strategy.TOMORROW, midday.date())


def test_midday_recovery_retries_when_refresh_failed_before_any_output() -> None:
    midday = datetime(2026, 8, 11, 12, 15, tzinfo=SHANGHAI)

    class FailingOnceData(DataRefresh):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        def refresh(self, request: V2CycleRequest) -> None:
            super().refresh(request)
            if request.strategy is Strategy.TOMORROW and not self.failed:
                self.failed = True
                raise V2DataRefreshUnavailableError("source_unavailable")

    data = FailingOnceData()
    index = UnifiedDecisionIndex()
    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=FixedClock(midday),
            calendar=TradingCalendar(),
            cadence=_cadence(midday),
            data=data,
            decisions=Decisions(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncDecisionObserver((), capacity=4, thread_name="test-v2-midday-retry-observer"),
            freezes=Freezes(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_decision=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-v2",
    )

    runtime.start()
    runtime.submit_due(midday)
    assert runtime.wait_idle(2.0)
    runtime.submit_due(midday + timedelta(seconds=30))
    assert runtime.wait_idle(2.0)
    runtime.stop(ShutdownDeadline.start(2.0))

    scoring_calls = [request.strategy for request in data.requests if request.phase != "quote_overlay"]
    assert scoring_calls.count(Strategy.TOMORROW) == 2
    assert scoring_calls.count(Strategy.D25) == 1
    assert scoring_calls.count(Strategy.LONG) == 2
    assert index.snapshot(Strategy.TOMORROW).current is not None


def test_midday_recovery_does_not_queue_duplicate_while_lane_is_active() -> None:
    midday = datetime(2026, 8, 11, 12, 15, tzinfo=SHANGHAI)
    entered = threading.Event()
    release = threading.Event()

    class BlockingData(DataRefresh):
        def refresh(self, request: V2CycleRequest) -> None:
            super().refresh(request)
            if request.strategy is Strategy.TOMORROW:
                entered.set()
                release.wait(timeout=1.0)

    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=FixedClock(midday),
            calendar=TradingCalendar(),
            cadence=_cadence(midday),
            data=BlockingData(),
            decisions=Decisions(),
            reviews=SharedReviews(),
            index=UnifiedDecisionIndex(),
            observer=AsyncDecisionObserver((), capacity=4, thread_name="test-v2-midday-active-observer"),
            freezes=Freezes(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_decision=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-v2",
    )

    runtime.start()
    runtime.submit_due(midday)
    assert entered.wait(timeout=1.0)
    runtime.submit_due(midday + timedelta(seconds=30))
    tomorrow_lane = next(lane for lane in runtime.status().lanes if lane.name == "trader-v2-tomorrow")
    release.set()
    assert runtime.wait_idle(2.0)
    runtime.stop(ShutdownDeadline.start(2.0))

    assert tomorrow_lane.running
    assert tomorrow_lane.offered_count == 1
    assert tomorrow_lane.pending is False


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
            cadence=_cadence(after_close),
            data=data,
            decisions=Decisions(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncDecisionObserver((), capacity=4, thread_name="test-v2-after-close-current-observer"),
            freezes=freezes,
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_decision=lambda _event: None,
            publish_overlay=lambda _overlay: None,
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

    assert [request.strategy for request in data.requests if request.phase != "quote_overlay"] == [Strategy.LONG]
    assert {(strategy, recovery_path) for strategy, recovery_path, _version in freezes.close_fallback_calls} == {
        (Strategy.TOMORROW, "current"),
        (Strategy.D25, "current"),
    }


def test_after_close_formal_records_only_refresh_selected_overlays_without_full_market_recovery() -> None:
    after_close = datetime(2026, 8, 11, 15, 5, tzinfo=SHANGHAI)
    data = DataRefresh()
    index = UnifiedDecisionIndex()
    settlement = Settlement()
    for strategy in (Strategy.TOMORROW, Strategy.D25):
        formal = replace(decision(strategy, sequence=1), trade_date=after_close.date())
        assert index.restore_formal(CommittedDecisionRecord(formal, after_close, "scheduled"))
    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=FixedClock(after_close),
            calendar=TradingCalendar(),
            cadence=_cadence(after_close),
            data=data,
            decisions=Decisions(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncDecisionObserver((), capacity=4, thread_name="test-v2-after-close-formal-observer"),
            freezes=Freezes(),
            settlement=settlement,
            research_factory=noop_research_factory,
            publish_decision=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-v2",
    )

    runtime.start()
    runtime.submit_due(after_close)
    assert runtime.wait_idle(2.0)
    runtime.stop(ShutdownDeadline.start(2.0))

    assert PipelineTask.CLOSE_QUOTES not in {request.task for request in data.task_requests}
    topk = next(request for request in data.task_requests if request.task is PipelineTask.TOPK_QUOTES)
    assert set(topk.selected_codes) == {"600001"}
    assert settlement.calls == [after_close]


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
            cadence=_cadence(NOW),
            data=DataRefresh(),
            decisions=BlockingDecisions(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncDecisionObserver((), capacity=1, thread_name="test-v2-reserved-observer"),
            freezes=Freezes(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_decision=lambda _event: None,
            publish_overlay=lambda _overlay: None,
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


def test_direct_decision_stream_delivery_survives_a_full_audit_observer_queue() -> None:
    audit_entered = threading.Event()
    audit_release = threading.Event()

    def blocked_audit(_observation) -> None:
        audit_entered.set()
        audit_release.wait(timeout=2.0)

    events = []
    index = UnifiedDecisionIndex()
    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=FixedClock(NOW),
            calendar=TradingCalendar(),
            cadence=_cadence(NOW),
            data=DataRefresh(),
            decisions=Decisions(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncDecisionObserver(
                (blocked_audit,),
                capacity=1,
                thread_name="test-v2-full-audit-observer",
            ),
            freezes=Freezes(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_decision=events.append,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-v2",
    )
    runtime.start()
    try:
        for sequence in (1, 3, 5):
            runtime.submit_cycle(
                V2CycleRequest(
                    Strategy.TOMORROW,
                    NOW.date(),
                    NOW + timedelta(milliseconds=sequence),
                    "afternoon",
                    sequence,
                    f"input:{sequence}",
                    False,
                    NOW,
                )
            )
            assert _wait_for(
                lambda expected=sequence: (
                    isinstance(index.snapshot(Strategy.TOMORROW).current, ScoredDecision)
                    and index.snapshot(Strategy.TOMORROW).current.sequence == expected
                )
            )
        assert audit_entered.wait(timeout=1.0)
        assert len(events) == 3
        assert runtime.status().observer_rejection_count >= 1
    finally:
        audit_release.set()
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
            cadence=_cadence(NOW),
            data=data,
            decisions=decisions,
            reviews=SharedReviews(),
            index=index,
            observer=AsyncDecisionObserver((), capacity=1, thread_name="test-v2-failure-observer"),
            freezes=Freezes(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_decision=lambda _event: None,
            publish_overlay=lambda _overlay: None,
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
            return {"status": "ready", "budget": {"limit": 168, "used": 0, "remaining": 168}}

    payload = runtime_status(runtime, StatusReviewer(), lambda: {})  # type: ignore[arg-type]
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
            cadence=_cadence(NOW),
            data=DataRefresh(),
            decisions=Decisions(),
            reviews=SharedReviews(),
            index=UnifiedDecisionIndex(),
            observer=AsyncDecisionObserver((), capacity=1, thread_name="test-v2-error-history-observer"),
            freezes=Freezes(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_decision=lambda _event: None,
            publish_overlay=lambda _overlay: None,
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
            cadence=_cadence(NOW),
            data=DataRefresh(),
            decisions=Decisions(),
            reviews=SharedReviews(),
            index=UnifiedDecisionIndex(),
            observer=AsyncDecisionObserver((), capacity=1, thread_name="test-v2-freeze-issue-observer"),
            freezes=Freezes(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_decision=lambda _event: None,
            publish_overlay=lambda _overlay: None,
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


def test_afternoon_schedule_skips_today_after_its_freeze_boundary() -> None:
    afternoon = datetime(2026, 8, 11, 13, 30, tzinfo=SHANGHAI)
    data = DataRefresh()
    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=FixedClock(afternoon),
            calendar=TradingCalendar(),
            cadence=_cadence(afternoon),
            data=data,
            decisions=Decisions(),
            reviews=SharedReviews(),
            index=UnifiedDecisionIndex(),
            observer=AsyncDecisionObserver((), capacity=1, thread_name="test-v2-afternoon-observer"),
            freezes=Freezes(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_decision=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-v2",
    )

    runtime.start()
    runtime.submit_due(afternoon)
    assert runtime.wait_idle(1.0)
    status = runtime.status()
    runtime.stop(ShutdownDeadline.start(1.0))

    assert set(data.calls) == {Strategy.TOMORROW, Strategy.D25, Strategy.LONG}
    assert status.publish_rejection_count == 0
    assert status.strategy_error_codes == ()


def test_expected_late_publish_rejection_after_freeze_is_not_a_runtime_error() -> None:
    before_freeze = datetime(2026, 8, 11, 11, 19, 59, tzinfo=SHANGHAI)
    after_freeze = datetime(2026, 8, 11, 11, 20, 1, tzinfo=SHANGHAI)
    index = UnifiedDecisionIndex()
    fixture = decision(Strategy.TODAY, sequence=1)
    fixture_quote = fixture.items[0].quote
    assert fixture_quote is not None
    current = replace(
        fixture,
        observed_at=before_freeze,
        items=(replace(fixture.items[0], quote=replace(fixture_quote, source_time=before_freeze)),),
    )
    assert index.publish(current, expected_version=None).accepted
    assert index.seal_for_freeze(
        Strategy.TODAY, boundary_at=before_freeze.replace(second=0) + timedelta(minutes=1)
    ).accepted
    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=FixedClock(after_freeze),
            calendar=TradingCalendar(),
            cadence=_cadence(after_freeze),
            data=DataRefresh(),
            decisions=Decisions(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncDecisionObserver((), capacity=1, thread_name="test-v2-late-today-observer"),
            freezes=Freezes(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_decision=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-v2",
    )
    request = V2CycleRequest(
        Strategy.TODAY,
        after_freeze.date(),
        after_freeze,
        "midday",
        3,
        "input-v2",
        False,
        before_freeze,
    )

    runtime.start()
    runtime.submit_cycle(request)
    assert runtime.wait_idle(1.0)
    status = runtime.status()
    runtime.stop(ShutdownDeadline.start(1.0))

    assert status.publish_rejection_count == 1
    assert status.strategy_error_codes == ()
    assert status.recent_errors == ()


def test_review_deadline_prevents_a_late_model_upgrade() -> None:
    reviews = SharedReviews()
    index = UnifiedDecisionIndex()
    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=FixedClock(NOW),
            calendar=TradingCalendar(),
            cadence=_cadence(NOW),
            data=DataRefresh(),
            decisions=Decisions(),
            reviews=reviews,
            index=index,
            observer=AsyncDecisionObserver((), capacity=1, thread_name="test-v2-deadline-observer"),
            freezes=Freezes(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_decision=lambda _event: None,
            publish_overlay=lambda _overlay: None,
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
