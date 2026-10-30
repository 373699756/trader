from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from tests.unit.domain.test_freeze_identity import NOW, freeze
from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.infra.published_history_snapshot import SQLitePublishedHistorySnapshot
from trader.infra.shutdown import ShutdownDeadline, ShutdownStep
from trader.infra.workers import BoundedExecutor
from trader.recommendation.application.pipeline.data_source.source_quality import FailureCode
from trader.recommendation.application.pipeline.freeze_publish.freeze_observers import AsyncFreezeObserver
from trader.recommendation.application.pipeline.freeze_publish.publication_io import PublicationIoTracker
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.application.ports.market_data import MarketDataPendingError
from trader.recommendation.application.ports.runtime import (
    CycleRequest,
    DataRefreshPendingError,
    DataRefreshUnavailableError,
    FreezeUnavailableError,
    RefreshOutcome,
    ResearchIntent,
    ResearchRuntimeStatus,
    SharedDeepSeekRuntimeContract,
    TradingCalendarUnavailableError,
)
from trader.recommendation.application.runtime.cadence import (
    CadencePlanner,
    CadencePolicy,
    PipelineTask,
    ScheduledPipelineTask,
    SchedulePointKey,
    SchedulePointStatus,
)
from trader.recommendation.application.runtime.latest_wins import LatestWinsOffer
from trader.recommendation.application.runtime.runtime_dependencies import RuntimeDependencies
from trader.recommendation.application.runtime.schedule import SHANGHAI, MarketPhase, SchedulePoint
from trader.recommendation.application.runtime.scheduler_runtime import SchedulerRuntime
from trader.recommendation.domain.publication.freeze_identity import (
    CommittedFreezeRecord,
    FreezeOverlay,
    LongProjection,
    LongProjectionItem,
    ScoredFreeze,
)
from trader.recommendation.domain.publication.models import RecommendationAction, Strategy
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache
from trader.recommendation.infra.status_projection import RuntimeStatus


class TradingCalendar:
    def is_trading_day(self, _day: date) -> bool:
        return True


class FixedClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.current.timestamp()


class DataRefresh:
    def __init__(self) -> None:
        self.calls: list[Strategy] = []
        self.requests: list[CycleRequest] = []
        self.task_requests: list[object] = []
        self.released_requests: list[CycleRequest] = []

    def refresh(self, request: CycleRequest) -> None:
        self.calls.append(request.strategy)
        self.requests.append(request)

    def scoring_epoch(self) -> str:
        return "test:shared_input"

    def refresh_pair(self, requests: tuple[CycleRequest, CycleRequest]) -> None:
        for request in requests:
            self.refresh(request)

    def release_scoring_input(self, request: CycleRequest) -> None:
        self.released_requests.append(request)

    def refresh_task(self, request) -> RefreshOutcome:
        self.task_requests.append(request)
        return RefreshOutcome(
            request.task,
            True,
            f"test:{request.task.value}:{request.observed_at:%H%M%S%f}",
            (),
            request.observed_at,
            False,
        )


class BlockingScoringInputs(DataRefresh):
    def __init__(self, *, block_full_market: bool = False) -> None:
        super().__init__()
        self.full_market_started = threading.Event()
        self.release_full_market = threading.Event()
        if not block_full_market:
            self.release_full_market.set()
        self.candidate_started = threading.Event()
        self.release_candidate = threading.Event()

    def refresh_task(self, request) -> RefreshOutcome:
        outcome = super().refresh_task(request)
        if request.task is PipelineTask.FULL_MARKET:
            self.full_market_started.set()
            assert self.release_full_market.wait(2.0)
        elif request.task is PipelineTask.CANDIDATE_QUOTES:
            self.candidate_started.set()
            assert self.release_candidate.wait(2.0)
        return outcome


def _cadence(at: datetime) -> CadencePlanner:
    return CadencePlanner(
        CadencePolicy.from_seconds(
            {
                "full_market": {"before_close": 10},
                "candidate_quotes": {"before_close": 10},
                "topk_quotes": {"before_close": 10, "after_close": 10},
                "long_quotes": {"before_close": 10, "after_close": 10},
                "score": {"before_close": 10},
                "intraday_tail": {"afternoon": 10, "final_review": 10},
                "industry_heat": {"warmup": 120, "morning": 60, "afternoon": 60, "final_review": 60},
            }
        ),
        started_at=at,
    )


class Freezes:
    def input_quality_status(self):
        return ()

    def has_local_draft(self, strategy: Strategy, trade_date: date) -> bool:
        del strategy, trade_date
        return False

    def build_local(self, request: CycleRequest):
        if request.strategy is Strategy.LONG:
            return LongProjection(
                request.trade_date,
                request.sequence,
                request.observed_at,
                (("quotes", request.input_version),),
                (LongProjectionItem("600001", "core", request.input_version),),
            )
        current = freeze(request.strategy, sequence=request.sequence)
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

    def initial_overlay(self, freeze: ScoredFreeze) -> FreezeOverlay:
        quotes = tuple(item.quote for item in freeze.items if item.selected and item.quote is not None)
        return FreezeOverlay(freeze.strategy, freeze.trade_date, freeze.version, freeze.observed_at, quotes, sequence=1)

    def refreshed_overlay(
        self,
        freeze: ScoredFreeze,
        request: CycleRequest,
        previous: FreezeOverlay | None,
    ) -> FreezeOverlay | None:
        del previous
        quotes = tuple(
            replace(
                item.quote,
                price=(item.quote.price or 0.0) + 1.0,
                source_time=request.observed_at,
                data_version=request.input_version,
            )
            for item in freeze.items
            if item.selected and item.quote is not None
        )
        if not quotes:
            return None
        return FreezeOverlay(
            freeze.strategy, freeze.trade_date, freeze.version, request.observed_at, quotes, sequence=1
        )

    def research_audit_factory(self, version: str):
        del version
        return lambda: None

    def tomorrow_profile_research_input(self, version: str):
        del version
        return None

    def research_intent(self, current: ScoredFreeze) -> ResearchIntent:
        codes = tuple(item.code for item in current.items)
        return ResearchIntent(current.strategy, current.trade_date, codes, codes)


class NoopResearchRuntime:
    def start(self) -> bool:
        return True

    def stop(self, *, wait: bool, deadline=None) -> ShutdownStep:
        del wait, deadline
        return ShutdownStep("research", completed=True, timed_out=False)

    def observe(self, intent, request) -> bool:
        del intent, request
        return False

    def wait_until_idle(self, timeout_seconds: float) -> bool:
        del timeout_seconds
        return True

    def status(self):
        return ResearchRuntimeStatus(state="idle")


def noop_research_factory():
    return NoopResearchRuntime()


class SharedReviews:
    runtime_contract = SharedDeepSeekRuntimeContract(
        daily_physical_limit=168,
        shared_cache=True,
        shared_single_flight=True,
    )

    def __init__(self) -> None:
        self.calls: list[Strategy] = []

    def build_hybrid(self, local: ScoredFreeze, request: CycleRequest) -> ScoredFreeze:
        self.calls.append(request.strategy)
        return replace(
            local,
            sequence=local.sequence + 1,
            stage="hybrid",
            parent_version=local.version,
            items=(replace(local.items[0], final_score=89.0),),
        )


@pytest.mark.parametrize("failure", ("overlay", "cas", "event", "observer"))
def test_publication_failure_receipts_reach_http_without_changing_valid_current(failure: str, tmp_path) -> None:
    from trader.http_api.route_services import UnifiedWebServices
    from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedFreezeDraftIndex
    from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedFreezeEventStream
    from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedFreezeQueries
    from trader.recommendation.infra.persistence.freeze_records import SQLiteFreezeRecords
    from trader.web import CreateApp

    clock = FixedClock(NOW)
    tracker = PublicationIoTracker(now=clock.now, monotonic=lambda: 1.0)
    index = UnifiedFreezeIndex()
    current = freeze()
    assert index.publish(current, expected_version=None).accepted
    builder = Freezes()

    class BrokenOverlay(Freezes):
        def initial_overlay(self, value: ScoredFreeze) -> FreezeOverlay:
            raise FreezeUnavailableError("controlled_overlay_failure")

    class StatusReviewer:
        def status(self):
            return {"budget": {"available": True, "limit": 168}}

    def event_sink(_event):
        if failure == "event":
            raise RuntimeError("secret external payload")

    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=clock,
            monotonic=clock.monotonic,
            calendar=TradingCalendar(),
            cadence=_cadence(NOW),
            data=DataRefresh(),
            freezes=BrokenOverlay() if failure == "overlay" else builder,
            reviews=SharedReviews(),
            index=index,
            observer=AsyncFreezeObserver((), capacity=1, thread_name="test-publication-failure"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=event_sink,
            publish_overlay=lambda _overlay: None,
            publication_io=tracker,
        ),
        config_version="runtime-current",
    )
    # Keeping the observer stopped deterministically models bounded queue rejection.
    candidate = (
        replace(current, sequence=3) if failure != "cas" else replace(current, sequence=1, degraded_reasons=("late",))
    )
    published = runtime._cycles._publish(candidate, hybrid=False)
    if failure in {"overlay", "cas"}:
        assert not published and index.snapshot(Strategy.TOMORROW).current == current
        operation = "current_publish"
    else:
        assert published and index.snapshot(Strategy.TOMORROW).current == candidate
        operation = "freeze_event" if failure == "event" else "observer_enqueue"
    payload = RuntimeStatus(runtime, StatusReviewer(), lambda: {})  # type: ignore[arg-type]
    assert payload["health"]["issue_count"] > 0
    repository = SQLiteFreezeRecords((tmp_path) / "freeze.sqlite3")
    repository.initialize()
    queries = UnifiedFreezeQueries(index, UnifiedFreezeDraftIndex(), repository, clock)
    stream = UnifiedFreezeEventStream()
    client = CreateApp(services=UnifiedWebServices(queries, stream, lambda: payload)).test_client()
    response = client.get("/api/status").get_json()
    receipt = next(item for item in response["scheduler"]["publication_io"] if item["operation"] == operation)
    assert receipt["input_count"] == receipt["failed_count"] == 1
    assert receipt["rejected_count"] == receipt["output_count"] == 0
    assert receipt["freeze_version"] == candidate.version
    assert receipt["count_unit"] == "operation" and "rejection_rate" not in receipt
    assert "secret" not in str(receipt) and "items" not in receipt
    assert current.content_hash == freeze().content_hash
    visible = client.get("/api/freezes/tomorrow/current").get_json()
    expected = current if failure in {"overlay", "cas"} else candidate
    assert visible["freeze_version"] == expected.version
    assert visible["content_hash"] is None


def test_scheduler_atomically_publishes_complete_quotes_for_local_and_hybrid() -> None:
    index = UnifiedFreezeIndex()
    tracker = PublicationIoTracker(now=lambda: NOW, monotonic=lambda: 1.0)
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=FixedClock(NOW),
            calendar=TradingCalendar(),
            cadence=_cadence(NOW),
            data=DataRefresh(),
            freezes=Freezes(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncFreezeObserver((), capacity=4, thread_name="test-overlay-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
            publication_io=tracker,
        ),
        config_version="runtime-current",
    )
    request = CycleRequest(
        Strategy.TOMORROW,
        NOW.date(),
        NOW,
        "afternoon",
        1,
        "input-initial",
        True,
        NOW + timedelta(minutes=1),
    )

    runtime.start()
    runtime.submit_cycle(request)
    assert runtime.wait_idle(2.0)
    runtime.stop(ShutdownDeadline.start(2.0))

    snapshot = index.snapshot(Strategy.TOMORROW)
    assert isinstance(snapshot.current, ScoredFreeze)
    assert snapshot.current.stage == "hybrid"
    receipts = runtime.status().publication_io
    assert {item.operation for item in receipts} == {"current_publish", "freeze_event", "observer_enqueue"}
    assert all(item.target.freeze_version == snapshot.current.version for item in receipts)
    assert all(item.output_count == 1 and item.rejected_count == item.failed_count == 0 for item in receipts)
    assert snapshot.overlay is not None
    assert snapshot.overlay.parent_version == snapshot.current.version
    assert snapshot.overlay.quotes == (snapshot.current.items[0].quote,)
    assert snapshot.overlay.quotes[0].amount == 120_000_000.0
    assert snapshot.overlay.quotes[0].turnover_rate == 2.1
    assert snapshot.overlay.quotes[0].market_cap == 12_000_000_000.0


@pytest.mark.parametrize("offer", [LatestWinsOffer.COALESCED, LatestWinsOffer.STALE, LatestWinsOffer.REJECTED])
def test_unqueued_wave_releases_round_admission(monkeypatch, offer) -> None:
    runtime = _round_cleanup_runtime()
    scheduled = ScheduledPipelineTask(PipelineTask.SCORE, NOW, MarketPhase.BEFORE_CLOSE)
    monkeypatch.setattr(runtime._score_lane, "offer", lambda _item: offer)
    assert not runtime._submit_round_tasks((scheduled,), scheduled)
    following = runtime._dependencies.rounds.enter(runtime._dependencies.round_profile, (scheduled,))
    assert following is not None
    runtime._dependencies.rounds.finish(runtime._dependencies.round_profile, following.sequence)


def test_stopping_queued_wave_releases_round_without_releasing_running_work() -> None:
    runtime = _round_cleanup_runtime()
    entered = threading.Event()
    release = threading.Event()

    def block(_item: ScheduledPipelineTask) -> None:
        entered.set()
        release.wait()

    runtime._score_lane._processor = block
    scheduled = ScheduledPipelineTask(PipelineTask.SCORE, NOW, MarketPhase.BEFORE_CLOSE)
    runtime._score_lane.start()
    try:
        runtime._score_lane.offer(scheduled)
        assert entered.wait(1.0)
        queued = replace(scheduled, scheduled_at=NOW + timedelta(seconds=1))
        assert runtime._submit_round_tasks((queued,), queued)
        rounds = runtime._dependencies.rounds
        profile = runtime._dependencies.round_profile
        runtime._score_lane.close()
        assert runtime._score_lane.status().running
        following = rounds.enter(profile, (queued,))
        assert following is not None
        rounds.finish(profile, following.sequence)
    finally:
        release.set()
        assert runtime._score_lane.stop(deadline=ShutdownDeadline.start(2.0)).completed


def _round_cleanup_runtime() -> SchedulerRuntime:
    return SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(worker_count=1, queue_capacity=4, thread_name_prefix="test-round-cleanup"),
            clock=FixedClock(NOW), calendar=TradingCalendar(), cadence=_cadence(NOW),
            data=DataRefresh(), freezes=Freezes(), reviews=SharedReviews(), index=UnifiedFreezeIndex(),
            observer=AsyncFreezeObserver((), capacity=4, thread_name="test-round-cleanup-observer"),
            sealing=Sealing(), settlement=Settlement(), research_factory=noop_research_factory,
            publish_freeze=lambda _event: None, publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )


def test_slow_hybrid_review_does_not_block_a_newer_local_score() -> None:
    review_started = threading.Event()
    release_review = threading.Event()

    class BlockingReviews(SharedReviews):
        def build_hybrid(self, local: ScoredFreeze, request: CycleRequest) -> ScoredFreeze:
            review_started.set()
            assert release_review.wait(2.0)
            return super().build_hybrid(local, request)

    index = UnifiedFreezeIndex()
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=FixedClock(NOW),
            calendar=TradingCalendar(),
            cadence=_cadence(NOW),
            data=DataRefresh(),
            freezes=Freezes(),
            reviews=BlockingReviews(),
            index=index,
            observer=AsyncFreezeObserver((), capacity=4, thread_name="test-async-review-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )
    first = CycleRequest(
        Strategy.TOMORROW,
        NOW.date(),
        NOW,
        "afternoon",
        1,
        "input-initial",
        True,
        NOW + timedelta(minutes=1),
    )
    second = replace(
        first,
        observed_at=NOW + timedelta(seconds=1),
        sequence=3,
        input_version="input-latest",
        allow_review=False,
    )

    runtime.start()
    try:
        runtime.submit_cycle(first)
        assert review_started.wait(1.0)
        runtime.submit_cycle(second)
        assert _wait_for(
            lambda: (
                isinstance(index.snapshot(Strategy.TOMORROW).current, ScoredFreeze)
                and index.snapshot(Strategy.TOMORROW).current.sequence == 3
            )
        )
    finally:
        release_review.set()
        assert runtime.wait_idle(2.0)
        runtime.stop(ShutdownDeadline.start(2.0))

    current = index.snapshot(Strategy.TOMORROW).current
    assert isinstance(current, ScoredFreeze)
    assert current.sequence == 3
    assert current.stage == "local"


def test_scheduler_refreshes_frozen_tomorrow_overlay_without_mutating_formal_freeze() -> None:
    observed_at = datetime(2026, 8, 11, 15, 5, tzinfo=SHANGHAI)
    frozen_at = observed_at.replace(hour=14, minute=50)
    index = UnifiedFreezeIndex()
    source = freeze(Strategy.TOMORROW, sequence=1)
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
    record = CommittedFreezeRecord(source, frozen_at, "scheduled")
    assert index.restore_formal(record)
    overlay_events: list[FreezeOverlay] = []
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=FixedClock(observed_at),
            calendar=TradingCalendar(),
            cadence=_cadence(observed_at),
            data=DataRefresh(),
            freezes=Freezes(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncFreezeObserver((), capacity=4, thread_name="test-frozen-overlay-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=overlay_events.append,
        ),
        config_version="runtime-current",
    )
    request = CycleRequest(
        Strategy.TOMORROW,
        observed_at.date(),
        observed_at,
        "afternoon",
        1,
        "quotes-current",
        False,
        observed_at,
    )

    runtime.start()
    runtime.submit_cycle(request)
    assert runtime.wait_idle(2.0)
    runtime.stop(ShutdownDeadline.start(2.0))

    snapshot = index.snapshot(Strategy.TOMORROW)
    assert snapshot.formal == record
    assert snapshot.current == record.freeze
    assert snapshot.overlay is not None
    assert snapshot.overlay.parent_version == record.freeze.version
    assert snapshot.overlay.quotes[0].price == (source_quote.price or 0.0) + 1.0
    assert overlay_events == [snapshot.overlay]


def test_frozen_cadence_targets_formal_codes_and_refreshes_overlay_without_rescoring() -> None:
    frozen_at = datetime(2026, 8, 11, 15, 5, tzinfo=SHANGHAI)
    index = UnifiedFreezeIndex()
    source = freeze(Strategy.TOMORROW, sequence=1)
    anchor = source.items[0].quote
    assert anchor is not None
    source = replace(
        source,
        trade_date=frozen_at.date(),
        observed_at=frozen_at.replace(hour=14, minute=59, second=59),
        items=(
            replace(
                source.items[0], quote=replace(anchor, source_time=frozen_at.replace(hour=14, minute=59, second=59))
            ),
        ),
    )
    visible_unselected = replace(
        source.items[0],
        code="600002",
        selected=False,
        rank=0,
        selection_rank=0,
        action=RecommendationAction.UNAVAILABLE,
        quote=replace(source.items[0].quote, code="600002"),
    )
    source = replace(source, items=(*source.items, visible_unselected))
    record = CommittedFreezeRecord(source, frozen_at.replace(hour=15, minute=0), "scheduled")
    assert index.restore_formal(record)
    d25_source = replace(
        freeze(Strategy.D25, sequence=1),
        trade_date=frozen_at.date(),
        observed_at=source.observed_at,
    )
    assert index.restore_formal(CommittedFreezeRecord(d25_source, record.committed_at, "scheduled"))
    data = DataRefresh()

    class RecordingOverlayFreezes(Freezes):
        def __init__(self) -> None:
            self.overlay_times: list[datetime] = []

        def refreshed_overlay(self, current, request, previous):
            self.overlay_times.append(request.observed_at)
            return super().refreshed_overlay(current, request, previous)

    freezes = RecordingOverlayFreezes()
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=FixedClock(frozen_at + timedelta(seconds=2)),
            calendar=TradingCalendar(),
            cadence=_cadence(frozen_at),
            data=data,
            freezes=freezes,
            reviews=SharedReviews(),
            index=index,
            observer=AsyncFreezeObserver((), capacity=4, thread_name="test-frozen-cadence-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )

    assert runtime._selected_overlay_codes(MarketPhase.BEFORE_CLOSE) == ("600001",)
    assert runtime._selected_overlay_codes(MarketPhase.BEFORE_CLOSE) == ("600001",)
    runtime.start()
    runtime.submit_due(frozen_at)
    assert runtime.wait_idle(2.0)
    runtime.stop(ShutdownDeadline.start(2.0))

    topk = next(request for request in data.task_requests if request.task is PipelineTask.TOPK_QUOTES)
    assert topk.selected_codes == ("600001",)
    assert topk.observed_at in freezes.overlay_times
    assert not [
        request
        for request in data.requests
        if request.strategy is not Strategy.LONG and request.phase != "quote_overlay"
    ]
    snapshot = index.snapshot(Strategy.TOMORROW)
    assert snapshot.formal == record
    assert snapshot.overlay is not None
    assert snapshot.overlay.quotes[0].price == (anchor.price or 0.0) + 1.0


def test_scheduler_recovers_overlay_issue_after_later_success() -> None:
    observed_at = datetime(2026, 8, 11, 13, 5, tzinfo=SHANGHAI)
    clock = FixedClock(observed_at)
    index = UnifiedFreezeIndex()
    source = freeze(Strategy.TOMORROW, sequence=1)
    source_quote = source.items[0].quote
    assert source_quote is not None
    source = replace(
        source,
        trade_date=observed_at.date(),
        observed_at=observed_at - timedelta(minutes=1),
        items=(replace(source.items[0], quote=replace(source_quote, source_time=observed_at - timedelta(minutes=1))),),
    )
    assert index.restore_formal(CommittedFreezeRecord(source, observed_at, "scheduled"))

    class FailingOnceOverlayFreezes(Freezes):
        def __init__(self) -> None:
            self.failed = False

        def refreshed_overlay(self, current, request, previous):
            if current.strategy is Strategy.TOMORROW and not self.failed:
                self.failed = True
                raise FreezeUnavailableError("overlay_source_unavailable")
            return super().refreshed_overlay(current, request, previous)

    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=clock,
            monotonic=clock.monotonic,
            calendar=TradingCalendar(),
            cadence=_cadence(observed_at),
            data=DataRefresh(),
            freezes=FailingOnceOverlayFreezes(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncFreezeObserver((), capacity=4, thread_name="test-overlay-recovery-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )
    first = CycleRequest(
        Strategy.TOMORROW,
        observed_at.date(),
        observed_at,
        "afternoon",
        1,
        "quotes-current",
        False,
        observed_at,
    )
    second_at = observed_at + timedelta(seconds=30)
    second = replace(first, observed_at=second_at, input_version="quotes-retry")

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
    assert dict(recovered.strategy_error_codes).get("tomorrow") is None


def test_scheduler_publishes_local_before_research_visibility_and_does_not_defer_review() -> None:
    index = UnifiedFreezeIndex()
    reviews = SharedReviews()
    holder: list[object] = []

    class RecordingResearchRuntime(NoopResearchRuntime):
        def __init__(self) -> None:
            self.observed_versions: list[str] = []

        def observe(self, intent, request) -> bool:
            del intent
            current = index.snapshot(request.strategy).current
            assert current is not None
            self.observed_versions.append(current.version)
            return False

    def research_factory():
        research = RecordingResearchRuntime()
        holder.append(research)
        return research

    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=FixedClock(NOW),
            calendar=TradingCalendar(),
            cadence=_cadence(NOW),
            data=DataRefresh(),
            freezes=Freezes(),
            reviews=reviews,
            index=index,
            observer=AsyncFreezeObserver((), capacity=4, thread_name="test-research-order-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )
    request = CycleRequest(
        Strategy.TOMORROW,
        NOW.date(),
        NOW,
        "morning_main",
        1,
        "input-initial",
        True,
        NOW + timedelta(minutes=1),
    )

    runtime.start()
    runtime.submit_cycle(request)
    assert runtime.wait_idle(2.0)
    research = holder[0]
    assert isinstance(research, RecordingResearchRuntime)
    assert reviews.calls == [Strategy.TOMORROW]
    assert len(research.observed_versions) == 1
    runtime.stop(ShutdownDeadline.start(2.0))

    current = index.snapshot(Strategy.TOMORROW).current
    assert isinstance(current, ScoredFreeze)
    assert current.stage == "hybrid"


class Sealing:
    def __init__(self, index: UnifiedFreezeIndex | None = None) -> None:
        self._index = index
        self.calls: list[tuple[Strategy, str | None]] = []
        self.close_fallback_calls: list[tuple[Strategy, str, str]] = []

    def freeze(self, strategy: Strategy, at: datetime, current) -> None:
        self.calls.append((strategy, current.version if current is not None else None))
        if self._index is None:
            return
        sealed = self._index.seal_for_freeze(strategy, boundary_at=at)
        assert sealed.accepted and sealed.freeze is not None
        assert self._index.commit_formal(CommittedFreezeRecord(sealed.freeze, at, "scheduled"))

    def freeze_close_fallback(
        self,
        strategy: Strategy,
        at: datetime,
        current,
        *,
        recovery_path: str,
        official_close_version: str,
    ) -> None:
        self.close_fallback_calls.append((strategy, recovery_path, official_close_version))
        if self._index is not None:
            sealed = self._index.seal_close_fallback(
                current, boundary_at=at, official_close_version=official_close_version
            )
            assert sealed.accepted and sealed.freeze is not None
            assert self._index.commit_formal(CommittedFreezeRecord(sealed.freeze, at, "close_fallback"))


class Settlement:
    def __init__(self) -> None:
        self.calls: list[datetime] = []

    def settle(self, at: datetime) -> None:
        self.calls.append(at)


def test_calendar_failure_fails_closed_with_backoff_and_observable_recovery() -> None:
    observed_at = datetime(2026, 8, 11, 10, 0, tzinfo=SHANGHAI)
    clock = FixedClock(observed_at)
    data = DataRefresh()
    sealing = Sealing()

    class RecoveringCalendar:
        def __init__(self) -> None:
            self.calls = 0

        def is_trading_day(self, _day: date) -> bool:
            self.calls += 1
            if self.calls < 6:
                raise TradingCalendarUnavailableError("provider timeout")
            return True

    calendar = RecoveringCalendar()
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=clock,
            monotonic=clock.monotonic,
            calendar=calendar,
            cadence=_cadence(observed_at),
            data=data,
            freezes=Freezes(),
            reviews=SharedReviews(),
            index=UnifiedFreezeIndex(),
            observer=AsyncFreezeObserver((), capacity=4, thread_name="test-calendar-observer"),
            sealing=sealing,
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )

    runtime.start()
    try:
        assert runtime.submit_due(observed_at) == 30.0
        failed = runtime.status()
        assert failed.calendar.state == "unavailable"
        assert failed.calendar.trade_date == observed_at.date()
        assert failed.calendar.is_trading_day is None
        assert failed.calendar.consecutive_failure_count == 1
        assert failed.calendar.next_retry_at == observed_at + timedelta(seconds=30)
        assert failed.last_error_code == "calendar:calendar_unavailable"
        assert data.task_requests == []
        assert sealing.calls == []

        class StatusReviewer:
            @staticmethod
            def status():
                return {"status": "ready", "budget": {"limit": 168, "used": 0, "remaining": 168}}

        payload = RuntimeStatus(runtime, StatusReviewer(), lambda: {})  # type: ignore[arg-type]
        assert payload["health"] == {"level": "degraded", "issue_count": 1}
        assert payload["scheduler"]["calendar"] == {  # type: ignore[index]
            "generation": 1,
            "discontinuity_reason": "startup",
            "state": "unavailable",
            "trade_date": observed_at.date().isoformat(),
            "is_trading_day": None,
            "consecutive_failure_count": 1,
            "next_retry_at": (observed_at + timedelta(seconds=30)).isoformat(),
        }

        clock.current = observed_at + timedelta(seconds=10)
        assert runtime.submit_due(clock.current) == 20.0
        assert calendar.calls == 1
        assert data.task_requests == []

        for offset, expected_delay, expected_calls in (
            (30, 60.0, 2),
            (90, 120.0, 3),
            (210, 300.0, 4),
            (510, 300.0, 5),
        ):
            clock.current = observed_at + timedelta(seconds=offset)
            assert runtime.submit_due(clock.current) == expected_delay
            assert calendar.calls == expected_calls
            assert runtime.status().calendar.next_retry_at == clock.current + timedelta(seconds=expected_delay)

        clock.current = observed_at + timedelta(seconds=810)
        runtime.submit_due(clock.current)
        recovered = runtime.status()
        assert calendar.calls == 6
        assert recovered.calendar.state == "ready"
        assert recovered.calendar.is_trading_day is True
        assert recovered.calendar.consecutive_failure_count == 0
        assert recovered.calendar.next_retry_at is None
        assert recovered.last_error_code == ""
        assert recovered.recent_errors[0].recovery_status == "recovered"
    finally:
        runtime.stop(ShutdownDeadline.start(2.0))


@pytest.mark.parametrize("block_full_market", (False, True))
def test_periodic_tick_does_not_score_until_a_scoring_input_finishes(block_full_market: bool) -> None:
    observed_at = datetime(2026, 8, 11, 10, 0, tzinfo=SHANGHAI)
    data = BlockingScoringInputs(block_full_market=block_full_market)
    index = UnifiedFreezeIndex()

    class TrackingFreezes(Freezes):
        def __init__(self) -> None:
            self.build_started = threading.Event()

        def build_local(self, request: CycleRequest):
            if request.strategy is not Strategy.LONG:
                self.build_started.set()
            return super().build_local(request)

    freezes = TrackingFreezes()
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=FixedClock(observed_at),
            calendar=TradingCalendar(),
            cadence=_cadence(observed_at),
            data=data,
            freezes=freezes,
            reviews=SharedReviews(),
            index=index,
            observer=AsyncFreezeObserver((), capacity=4, thread_name="test-input-trigger-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )

    runtime.start()
    try:
        runtime.submit_due(observed_at)
        if block_full_market:
            assert data.full_market_started.wait(1.0)
            assert not data.candidate_started.is_set()
            data.release_full_market.set()
        assert data.candidate_started.wait(1.0)
        runtime.submit_due(observed_at + timedelta(seconds=2))
        candidate_lane = next(
            lane for lane in runtime.status().task_lanes if lane.name == "trader-task-score-wave"
        )
        assert candidate_lane.running
        assert not candidate_lane.pending
        assert not freezes.build_started.wait(0.2)
        assert index.snapshot(Strategy.TOMORROW).current is None

        data.release_candidate.set()
        assert runtime.wait_idle(2.0)
    finally:
        data.release_full_market.set()
        data.release_candidate.set()
        runtime.stop(ShutdownDeadline.start(2.0))

    assert isinstance(index.snapshot(Strategy.TOMORROW).current, ScoredFreeze)
    assert isinstance(index.snapshot(Strategy.D25).current, ScoredFreeze)


def test_scoring_wave_retains_both_previous_currents_when_d25_is_not_ready() -> None:
    observed_at = datetime(2026, 8, 11, 10, 0, tzinfo=SHANGHAI)
    index = UnifiedFreezeIndex()
    originals = (freeze(Strategy.TOMORROW), freeze(Strategy.D25))
    for original in originals:
        assert index.publish(original, expected_version=None).accepted

    class FailedD25(Freezes):
        def build_local(self, request: CycleRequest):
            if request.strategy is Strategy.D25:
                raise FreezeUnavailableError("model_input_pending")
            return super().build_local(request)

    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=FixedClock(observed_at),
            calendar=TradingCalendar(),
            cadence=_cadence(observed_at),
            data=DataRefresh(),
            freezes=FailedD25(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncFreezeObserver((), capacity=4, thread_name="test-paired-failure-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )
    runtime.start()
    try:
        runtime.submit_due(observed_at)
        assert runtime.wait_idle(2.0)
    finally:
        runtime.stop(ShutdownDeadline.start(2.0))
    assert tuple(index.snapshot(item.strategy).current for item in originals) == originals


def test_scoring_wave_uses_captured_pair_when_live_source_advances() -> None:
    observed_at = datetime(2026, 8, 11, 10, 0, tzinfo=SHANGHAI)
    index = UnifiedFreezeIndex()
    originals = (freeze(Strategy.TOMORROW), freeze(Strategy.D25))
    for original in originals:
        assert index.publish(original, expected_version=None).accepted

    class ChangingData(DataRefresh):
        def __init__(self) -> None:
            super().__init__()
            self.epoch = "initial"

        def scoring_epoch(self) -> str:
            return self.epoch

        def refresh(self, request: CycleRequest) -> None:
            super().refresh(request)
            if request.strategy is Strategy.D25:
                self.epoch = "new-input"

    data = ChangingData()

    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=FixedClock(observed_at),
            calendar=TradingCalendar(),
            cadence=_cadence(observed_at),
            data=data,
            freezes=Freezes(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncFreezeObserver((), capacity=4, thread_name="test-source-epoch-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )
    runtime.start()
    try:
        runtime.submit_due(observed_at)
        assert runtime.wait_idle(2.0)
    finally:
        runtime.stop(ShutdownDeadline.start(2.0))
    assert all(index.snapshot(item.strategy).current != item for item in originals)
    assert runtime.status().last_error_code != "freeze:paired_input_epoch_mismatch"
    assert all(request in data.released_requests for request in data.requests)


@pytest.mark.parametrize("finish_after_close", (False, True))
def test_scoring_wave_blocks_next_collection_until_current_publication_finishes(finish_after_close) -> None:
    observed_at = datetime(2026, 8, 11, 14, 59, 49, tzinfo=SHANGHAI)
    clock = FixedClock(observed_at)
    entered = threading.Event()
    release = threading.Event()

    class ChangingAfterCutData(DataRefresh):
        def __init__(self) -> None:
            super().__init__()
            self.epoch = "initial"

        def scoring_epoch(self) -> str:
            return self.epoch

    data = ChangingAfterCutData()

    class BlockingD25(Freezes):
        def __init__(self) -> None:
            self.built: list[Strategy] = []

        def build_local(self, request: CycleRequest):
            if request.strategy is Strategy.LONG:
                return super().build_local(request)
            if request.strategy is Strategy.D25:
                entered.set()
                assert release.wait(2.0)
            self.built.append(request.strategy)
            local = super().build_local(request)
            assert isinstance(local, ScoredFreeze)
            return replace(local, input_versions=(("market", f"native-market:{request.strategy.value}"),))

    freezes = BlockingD25()
    index = UnifiedFreezeIndex()
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=clock,
            calendar=TradingCalendar(),
            cadence=_cadence(observed_at),
            data=data,
            freezes=freezes,
            reviews=SharedReviews(),
            index=index,
            observer=AsyncFreezeObserver((), capacity=4, thread_name="test-overlap-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )
    runtime.start()
    try:
        runtime._dispatch_pipeline_task(
            ScheduledPipelineTask(PipelineTask.SCORE, observed_at, MarketPhase.BEFORE_CLOSE)
        )
        assert entered.wait(1.0)
        runtime._dispatch_pipeline_task(
            ScheduledPipelineTask(PipelineTask.SCORE, observed_at + timedelta(seconds=10), MarketPhase.BEFORE_CLOSE)
        )
        runtime.submit_due(observed_at + timedelta(seconds=1))
        assert data.task_requests == []
        data.epoch = "new-input-after-cut"
        score_lane = next(lane for lane in runtime.status().task_lanes if lane.name == "trader-task-score-wave")
        assert score_lane.offered_count == 1
        assert score_lane.running and not score_lane.pending
        if finish_after_close:
            clock.current = observed_at.replace(hour=15, minute=0, second=0)
        release.set()
        assert runtime.wait_idle(2.0)
    finally:
        release.set()
        runtime.stop(ShutdownDeadline.start(2.0))
    expected = [Strategy.TOMORROW, Strategy.D25]
    assert freezes.built == expected
    if finish_after_close:
        assert all(index.snapshot(strategy).current is None for strategy in expected)
    else:
        assert all(isinstance(index.snapshot(strategy).current, ScoredFreeze) for strategy in expected)


@pytest.mark.parametrize("task", (PipelineTask.CANDIDATE_QUOTES, PipelineTask.INTRADAY_TAIL))
@pytest.mark.parametrize("changed", (False, True))
def test_scoring_input_refresh_triggers_score_only_when_changed(task, changed) -> None:
    observed_at = datetime(2026, 8, 11, 14, 0, tzinfo=SHANGHAI)

    class UnchangedCandidates(DataRefresh):
        def refresh_task(self, request) -> RefreshOutcome:
            outcome = super().refresh_task(request)
            if request.task is task:
                return replace(outcome, changed=changed, data_version="input:stable")
            return outcome

    data = UnchangedCandidates()
    freezes = Freezes()
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=FixedClock(observed_at),
            calendar=TradingCalendar(),
            cadence=_cadence(observed_at),
            data=data,
            freezes=freezes,
            reviews=SharedReviews(),
            index=UnifiedFreezeIndex(),
            observer=AsyncFreezeObserver((), capacity=4, thread_name="test-unchanged-input-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )

    runtime.start()
    runtime._dispatch_pipeline_task(
        ScheduledPipelineTask(task, observed_at, MarketPhase.BEFORE_CLOSE)
    )
    assert runtime.wait_idle(2.0)
    runtime.stop(ShutdownDeadline.start(2.0))

    assert freezes.input_quality_status() == ()
    assert data.calls == ([Strategy.TOMORROW, Strategy.D25] if changed else [])


def test_current_fixture_runs_without_the_legacy_pipeline_through_shutdown() -> None:
    morning = datetime(2026, 8, 11, 10, 0, tzinfo=SHANGHAI)
    final_window_at = datetime(2026, 8, 11, 14, 59, tzinfo=SHANGHAI)
    close_at = datetime(2026, 8, 11, 15, 0, tzinfo=SHANGHAI)
    clock = FixedClock(morning)
    data = DataRefresh()
    reviews = SharedReviews()
    settlement = Settlement()
    observed: list[str] = []
    observer = AsyncFreezeObserver(
        (lambda observation: observed.append(observation.event.event_id),),
        capacity=16,
        thread_name="test-fixture-observer",
    )
    index = UnifiedFreezeIndex()
    sealing = Sealing(index)
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=clock,
            monotonic=clock.monotonic,
            calendar=TradingCalendar(),
            cadence=_cadence(morning),
            data=data,
            freezes=Freezes(),
            reviews=reviews,
            index=index,
            observer=observer,
            sealing=sealing,
            settlement=settlement,
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )

    assert runtime.start()
    try:
        runtime.submit_due()
        assert runtime.wait_idle(2.0)
        assert len(observed) == 4
        assert len(reviews.calls) == 2
        clock.current = final_window_at
        runtime.submit_due(final_window_at)
        runtime.submit_due(final_window_at)
        assert runtime.wait_idle(2.0)
        assert sealing.calls == []
        assert len(observed) == 8
        assert len(reviews.calls) == 4
        final_versions = {
            strategy: index.snapshot(strategy).current.version for strategy in (Strategy.TOMORROW, Strategy.D25)
        }
        clock.current = close_at
        runtime.submit_due(close_at)
        assert runtime.wait_idle(2.0)
        assert dict(sealing.calls) == final_versions
        assert all(
            index.snapshot(strategy).formal.commit_kind == "scheduled" for strategy in (Strategy.TOMORROW, Strategy.D25)
        )
        clock.current = close_at + timedelta(seconds=1)
        runtime.submit_due(clock.current)
        runtime.submit_due(clock.current)
        assert runtime.wait_idle(2.0)
        status = runtime.status()
    finally:
        report = runtime.stop(ShutdownDeadline.start(2.0))

    assert report.completed
    assert set(data.calls) == {Strategy.TOMORROW, Strategy.D25, Strategy.LONG}
    assert set(reviews.calls) == {Strategy.TOMORROW, Strategy.D25}
    assert len(observed) == 8
    assert {strategy for strategy, version in sealing.calls if version} == {Strategy.TOMORROW, Strategy.D25}
    assert sealing.close_fallback_calls == []
    assert settlement.calls == [close_at + timedelta(seconds=1)]
    assert status.freeze_completed_count == 2
    assert status.settlement_completed_count == 1
    assert status.phase is MarketPhase.AFTER_CLOSE
    assert status.config_version == "runtime-current"
    assert all(index.snapshot(strategy).current is not None for strategy in Strategy)
    assert not any(thread.name.startswith("trader-") for thread in threading.enumerate())
    assert runtime.submit_due(close_at) == 30.0
    assert runtime.status().control_rejected_count == status.control_rejected_count


@pytest.mark.parametrize("hour,minute,second", ((15, 0, 0), (15, 0, 1), (15, 5, 0), (19, 30, 0)))
def test_after_close_cold_start_recovers_missing_scored_strategies_and_long(hour, minute, second) -> None:
    after_close = datetime(2026, 8, 11, hour, minute, second, tzinfo=SHANGHAI)
    data = DataRefresh()
    freezes = Freezes()
    reviews = SharedReviews()
    settlement = Settlement()
    index = UnifiedFreezeIndex()
    sealing = Sealing(index)
    clock = FixedClock(after_close)
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=clock,
            monotonic=clock.monotonic,
            calendar=TradingCalendar(),
            cadence=_cadence(after_close),
            data=data,
            freezes=freezes,
            reviews=reviews,
            index=index,
            observer=AsyncFreezeObserver((), capacity=4, thread_name="test-after-close-observer"),
            sealing=sealing,
            settlement=settlement,
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )

    runtime.start()
    try:
        runtime.submit_due(after_close)
        assert runtime.wait_idle(2.0)
        formal = {strategy: index.snapshot(strategy).formal for strategy in (Strategy.TOMORROW, Strategy.D25)}
        assert all(record is not None and record.commit_kind == "close_fallback" for record in formal.values())
        clock.current = after_close + timedelta(seconds=10)
        runtime.submit_due(clock.current)
        assert runtime.wait_idle(2.0)
        assert {strategy: index.snapshot(strategy).formal for strategy in formal} == formal
        assert index.snapshot(Strategy.LONG).formal is None
    finally:
        runtime.stop(ShutdownDeadline.start(2.0))

    assert set(data.calls) == {Strategy.TOMORROW, Strategy.D25, Strategy.LONG}
    assert reviews.calls == []
    assert {(strategy, recovery_path) for strategy, recovery_path, _version in sealing.close_fallback_calls} == {
        (Strategy.TOMORROW, "close_rebuild"),
        (Strategy.D25, "close_rebuild"),
    }
    assert all(version.startswith("official-close:") for _strategy, _path, version in sealing.close_fallback_calls)
    assert all(index.snapshot(strategy).current is not None for strategy in (Strategy.TOMORROW, Strategy.D25))
    assert index.snapshot(Strategy.LONG).current is not None
    assert settlement.calls == [after_close + timedelta(seconds=10)]
    assert sum(request.task is PipelineTask.CLOSE_QUOTES for request in data.task_requests) == 1
    assert sealing.calls == []


@pytest.mark.parametrize("loading_history", (False, True))
@pytest.mark.parametrize("hour,minute,second", ((15, 0, 0), (15, 0, 1), (15, 5, 0), (19, 30, 0)))
def test_after_close_source_failure_retries_before_publishing_formal_records(
    tmp_path: Path,
    loading_history: bool,
    hour: int,
    minute: int,
    second: int,
) -> None:
    after_close = datetime(2026, 8, 11, hour, minute, second, tzinfo=SHANGHAI)
    history = PublishedHistoryCache(
        ReadPublishedHistoryUseCase(SQLitePublishedHistorySnapshot(tmp_path / "history")),
        lookback_sessions=61,
        monotonic=time.monotonic,
    )
    history.record_maintenance("loading", stage="reading_published_snapshot")

    class FailingOnceCloseData(DataRefresh):
        def refresh_task(self, request) -> RefreshOutcome:
            outcome = super().refresh_task(request)
            if request.task is PipelineTask.CLOSE_QUOTES:
                attempts = sum(item.task is PipelineTask.CLOSE_QUOTES for item in self.task_requests)
                if attempts == 1:
                    if loading_history:
                        try:
                            history.load(("600001",))
                        except MarketDataPendingError as exc:
                            raise DataRefreshPendingError(FailureCode(exc)) from exc
                    raise DataRefreshUnavailableError("controlled_close_source_failure")
            return outcome

    data = FailingOnceCloseData()
    reviews = SharedReviews()
    settlement = Settlement()
    index = UnifiedFreezeIndex()
    sealing = Sealing(index)
    clock = FixedClock(after_close)
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=clock,
            monotonic=clock.monotonic,
            calendar=TradingCalendar(),
            cadence=_cadence(after_close),
            data=data,
            freezes=Freezes(),
            reviews=reviews,
            index=index,
            observer=AsyncFreezeObserver((), capacity=4, thread_name="test-close-retry-observer"),
            sealing=sealing,
            settlement=settlement,
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )
    close_key = SchedulePointKey(after_close.date().isoformat(), SchedulePoint.CLOSE_QUOTES, "-")

    runtime.start()
    try:
        runtime.submit_due(after_close)
        assert runtime.wait_idle(2.0)
        failed = runtime.status()
        assert failed.refresh_failure_count == (0 if loading_history else 1)
        assert failed.cadence.schedule_points[close_key].status is SchedulePointStatus.RETRY_WAIT
        assert all(index.snapshot(strategy).formal is None for strategy in (Strategy.TOMORROW, Strategy.D25))
        assert index.snapshot(Strategy.LONG).current is not None
        assert sealing.close_fallback_calls == []
        assert settlement.calls == []

        clock.current = after_close + timedelta(milliseconds=999)
        runtime.submit_due(clock.current)
        assert runtime.wait_idle(2.0)
        assert sum(item.task is PipelineTask.CLOSE_QUOTES for item in data.task_requests) == 1
        clock.current = after_close + timedelta(seconds=1)
        runtime.submit_due(clock.current)
        assert runtime.wait_idle(2.0)
        formal = {strategy: index.snapshot(strategy).formal for strategy in (Strategy.TOMORROW, Strategy.D25)}
        assert all(record is not None and record.commit_kind == "close_fallback" for record in formal.values())
        assert runtime.status().cadence.schedule_points[close_key].status is SchedulePointStatus.RETRY_WAIT
        clock.current = after_close + timedelta(seconds=10)
        runtime.submit_due(clock.current)
        assert runtime.wait_idle(2.0)
        assert {strategy: index.snapshot(strategy).formal for strategy in formal} == formal
        assert runtime.status().cadence.schedule_points[close_key].status is SchedulePointStatus.COMPLETED
    finally:
        runtime.stop(ShutdownDeadline.start(2.0))

    assert sum(item.task is PipelineTask.CLOSE_QUOTES for item in data.task_requests) == 2
    assert len(sealing.close_fallback_calls) == 2
    assert sealing.calls == []
    assert reviews.calls == []
    assert index.snapshot(Strategy.LONG).formal is None
    assert settlement.calls == [after_close + timedelta(seconds=10)]


def test_midday_continues_ten_second_scoring_and_review() -> None:
    midday = datetime(2026, 8, 11, 12, 15, tzinfo=SHANGHAI)
    clock = FixedClock(midday)
    data = DataRefresh()
    reviews = SharedReviews()
    index = UnifiedFreezeIndex()
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=clock,
            calendar=TradingCalendar(),
            cadence=_cadence(midday),
            data=data,
            freezes=Freezes(),
            reviews=reviews,
            index=index,
            observer=AsyncFreezeObserver((), capacity=4, thread_name="test-midday-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )

    runtime.start()
    runtime.submit_due(midday)
    assert runtime.wait_idle(2.0)
    clock.current = midday + timedelta(seconds=10)
    runtime.submit_due(clock.current)
    assert runtime.wait_idle(2.0)
    runtime.stop(ShutdownDeadline.start(2.0))

    scoring_calls = [request.strategy for request in data.requests if request.phase != "quote_overlay"]
    assert {
        strategy: scoring_calls.count(strategy) for strategy in (Strategy.TOMORROW, Strategy.D25, Strategy.LONG)
    } == {
        Strategy.TOMORROW: 2,
        Strategy.D25: 2,
        Strategy.LONG: 2,
    }
    assert {request.phase for request in data.requests} <= {"before_close", "quote_overlay"}
    assert all(request.allow_review for request in data.requests if request.strategy is not Strategy.LONG)
    assert set(reviews.calls) == {Strategy.TOMORROW, Strategy.D25}
    assert all(index.snapshot(strategy).current is not None for strategy in Strategy)


def test_midday_pending_draft_does_not_disable_later_scoring() -> None:
    midday = datetime(2026, 8, 11, 12, 15, tzinfo=SHANGHAI)
    clock = FixedClock(midday)

    class DraftOnlyFreezes(Freezes):
        def __init__(self) -> None:
            self.drafts: set[tuple[Strategy, date]] = set()

        def has_local_draft(self, strategy: Strategy, trade_date: date) -> bool:
            return (strategy, trade_date) in self.drafts

        def build_local(self, request: CycleRequest):
            if request.strategy is Strategy.TOMORROW:
                self.drafts.add((request.strategy, request.trade_date))
                raise FreezeUnavailableError("security_master_coverage_incomplete")
            return super().build_local(request)

    data = DataRefresh()
    freezes = DraftOnlyFreezes()
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=clock,
            calendar=TradingCalendar(),
            cadence=_cadence(midday),
            data=data,
            freezes=freezes,
            reviews=SharedReviews(),
            index=UnifiedFreezeIndex(),
            observer=AsyncFreezeObserver((), capacity=4, thread_name="test-midday-draft-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )

    runtime.start()
    runtime.submit_due(midday)
    assert runtime.wait_idle(2.0)
    clock.current = midday + timedelta(seconds=10)
    runtime.submit_due(clock.current)
    assert runtime.wait_idle(2.0)
    runtime.stop(ShutdownDeadline.start(2.0))

    assert data.calls.count(Strategy.TOMORROW) == 2
    assert freezes.has_local_draft(Strategy.TOMORROW, midday.date())


def test_midday_continuous_loop_retries_failed_input() -> None:
    midday = datetime(2026, 8, 11, 12, 15, tzinfo=SHANGHAI)
    clock = FixedClock(midday)

    class FailingOnceData(DataRefresh):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        def refresh(self, request: CycleRequest) -> None:
            super().refresh(request)
            if request.strategy is Strategy.TOMORROW and not self.failed:
                self.failed = True
                raise DataRefreshUnavailableError("source_unavailable")

    data = FailingOnceData()
    index = UnifiedFreezeIndex()
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=clock,
            calendar=TradingCalendar(),
            cadence=_cadence(midday),
            data=data,
            freezes=Freezes(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncFreezeObserver((), capacity=4, thread_name="test-midday-retry-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )

    runtime.start()
    runtime.submit_due(midday)
    assert runtime.wait_idle(2.0)
    clock.current = midday + timedelta(seconds=10)
    runtime.submit_due(clock.current)
    assert runtime.wait_idle(2.0)
    runtime.stop(ShutdownDeadline.start(2.0))

    scoring_calls = [request.strategy for request in data.requests if request.phase != "quote_overlay"]
    assert scoring_calls.count(Strategy.TOMORROW) == 2
    assert scoring_calls.count(Strategy.D25) == 1
    assert scoring_calls.count(Strategy.LONG) == 2
    assert index.snapshot(Strategy.TOMORROW).current is not None


def test_midday_recovery_does_not_queue_duplicate_while_lane_is_running() -> None:
    midday = datetime(2026, 8, 11, 12, 15, tzinfo=SHANGHAI)
    clock = FixedClock(midday)
    entered = threading.Event()
    release = threading.Event()

    class BlockingData(DataRefresh):
        def refresh(self, request: CycleRequest) -> None:
            super().refresh(request)
            if request.strategy is Strategy.TOMORROW:
                entered.set()
                release.wait(timeout=1.0)

    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=clock,
            calendar=TradingCalendar(),
            cadence=_cadence(midday),
            data=BlockingData(),
            freezes=Freezes(),
            reviews=SharedReviews(),
            index=UnifiedFreezeIndex(),
            observer=AsyncFreezeObserver((), capacity=4, thread_name="test-midday-active-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )

    runtime.start()
    runtime.submit_due(midday)
    assert entered.wait(timeout=1.0)
    clock.current = midday + timedelta(seconds=10)
    runtime.submit_due(clock.current)
    score_lane = runtime._score_lane.status()
    release.set()
    assert runtime.wait_idle(2.0)
    runtime.stop(ShutdownDeadline.start(2.0))

    assert score_lane.running
    assert score_lane.offered_count == 1
    assert score_lane.pending is False


def test_after_close_prefers_existing_same_day_current_without_rebuilding() -> None:
    before_close = datetime(2026, 8, 11, 14, 49, 55, tzinfo=SHANGHAI)
    after_close = datetime(2026, 8, 11, 15, 5, tzinfo=SHANGHAI)
    data = DataRefresh()
    index = UnifiedFreezeIndex()
    sealing = Sealing(index)
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=FixedClock(after_close),
            calendar=TradingCalendar(),
            cadence=_cadence(after_close),
            data=data,
            freezes=Freezes(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncFreezeObserver((), capacity=4, thread_name="test-after-close-current-observer"),
            sealing=sealing,
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )
    for strategy in (Strategy.TOMORROW, Strategy.D25):
        current = replace(
            freeze(strategy, sequence=1),
            trade_date=before_close.date(),
            observed_at=before_close,
        )
        assert index.publish(current, expected_version=None).accepted

    runtime.start()
    runtime.submit_due(after_close)
    assert runtime.wait_idle(2.0)
    runtime.stop(ShutdownDeadline.start(2.0))

    assert [request.strategy for request in data.requests if request.phase != "quote_overlay"] == [Strategy.LONG]
    assert {(strategy, recovery_path) for strategy, recovery_path, _version in sealing.close_fallback_calls} == {
        (Strategy.TOMORROW, "current"),
        (Strategy.D25, "current"),
    }
    assert all(
        index.snapshot(strategy).formal.commit_kind == "close_fallback"
        for strategy in (Strategy.TOMORROW, Strategy.D25)
    )
    assert index.snapshot(Strategy.LONG).formal is None


def test_after_close_formal_records_only_refresh_selected_overlays_without_full_market_recovery() -> None:
    after_close = datetime(2026, 8, 11, 15, 5, tzinfo=SHANGHAI)
    data = DataRefresh()
    index = UnifiedFreezeIndex()
    settlement = Settlement()
    for strategy in (Strategy.TOMORROW, Strategy.D25):
        formal = replace(freeze(strategy, sequence=1), trade_date=after_close.date())
        assert index.restore_formal(CommittedFreezeRecord(formal, after_close, "scheduled"))
    existing = {strategy: index.snapshot(strategy).formal for strategy in (Strategy.TOMORROW, Strategy.D25)}
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=FixedClock(after_close),
            calendar=TradingCalendar(),
            cadence=_cadence(after_close),
            data=data,
            freezes=Freezes(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncFreezeObserver((), capacity=4, thread_name="test-after-close-formal-observer"),
            sealing=Sealing(),
            settlement=settlement,
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )

    runtime.start()
    runtime.submit_due(after_close)
    assert runtime.wait_idle(2.0)
    runtime.stop(ShutdownDeadline.start(2.0))

    assert PipelineTask.CLOSE_QUOTES not in {request.task for request in data.task_requests}
    topk = next(request for request in data.task_requests if request.task is PipelineTask.TOPK_QUOTES)
    assert set(topk.selected_codes) == {"600001"}
    assert settlement.calls == [after_close]
    assert {strategy: index.snapshot(strategy).formal for strategy in existing} == existing
    assert index.snapshot(Strategy.LONG).current is not None
    assert index.snapshot(Strategy.LONG).formal is None


def test_tomorrow_lane_progresses_while_d25_lane_is_blocked() -> None:
    d25_entered = threading.Event()
    d25_release = threading.Event()

    class BlockingFreezes(Freezes):
        def build_local(self, request: CycleRequest):
            if request.strategy is Strategy.D25:
                d25_entered.set()
                d25_release.wait(timeout=1.0)
            return super().build_local(request)

    index = UnifiedFreezeIndex()
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=FixedClock(NOW),
            calendar=TradingCalendar(),
            cadence=_cadence(NOW),
            data=DataRefresh(),
            freezes=BlockingFreezes(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncFreezeObserver((), capacity=1, thread_name="test-reserved-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )
    runtime.start()
    d25 = CycleRequest(Strategy.D25, NOW.date(), NOW, "afternoon", 1, "d25-fixture", True, NOW)
    tomorrow = replace(d25, strategy=Strategy.TOMORROW, input_version="tomorrow-fixture")
    runtime.submit_cycle(d25)
    assert d25_entered.wait(timeout=1.0)
    runtime.submit_cycle(tomorrow)

    try:
        assert _wait_for(lambda: index.snapshot(Strategy.TOMORROW).current is not None)
        assert index.snapshot(Strategy.D25).current is None
    finally:
        d25_release.set()
        runtime.stop(ShutdownDeadline.start(2.0))


def test_newer_same_strategy_cycle_does_not_starve_first_current_publication() -> None:
    first_entered = threading.Event()
    release_first = threading.Event()

    class BlockingFirstFreeze(Freezes):
        def build_local(self, request: CycleRequest):
            if request.sequence == 1:
                first_entered.set()
                release_first.wait(timeout=1.0)
            return super().build_local(request)

    index = UnifiedFreezeIndex()
    published = []
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=FixedClock(NOW),
            calendar=TradingCalendar(),
            cadence=_cadence(NOW),
            data=DataRefresh(),
            freezes=BlockingFirstFreeze(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncFreezeObserver((), capacity=2, thread_name="test-superseded-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=published.append,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )
    first = CycleRequest(Strategy.TOMORROW, NOW.date(), NOW, "afternoon", 1, "input-initial", False, NOW)
    second = replace(
        first,
        observed_at=NOW + timedelta(seconds=1),
        sequence=3,
        input_version="input-latest",
    )
    runtime.start()
    runtime.submit_cycle(first)
    assert first_entered.wait(timeout=1.0)
    runtime.submit_cycle(second)
    release_first.set()

    try:
        assert runtime.wait_idle(2.0)
        current = index.snapshot(Strategy.TOMORROW).current
        assert isinstance(current, ScoredFreeze)
        assert current.sequence == 3
        assert [event.projection.sequence for event in published if event.projection is not None] == [1, 3]
        assert runtime.status().local_publish_count == 2
    finally:
        release_first.set()
        runtime.stop(ShutdownDeadline.start(2.0))


def test_first_completed_current_can_freeze_while_newer_cycle_is_still_scoring() -> None:
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    release_second = threading.Event()
    observed_at = datetime(2026, 8, 11, 14, 59, 30, tzinfo=SHANGHAI)
    freeze_at = observed_at.replace(hour=15, minute=0, second=0)

    class BlockingBothFreezes(Freezes):
        def build_local(self, request: CycleRequest):
            if request.sequence == 1:
                first_entered.set()
                release_first.wait(timeout=2.0)
            elif request.sequence == 2:
                second_entered.set()
                release_second.wait(timeout=2.0)
            return super().build_local(request)

    clock = FixedClock(observed_at)
    index = UnifiedFreezeIndex()
    published = []
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=clock,
            monotonic=clock.monotonic,
            calendar=TradingCalendar(),
            cadence=_cadence(observed_at),
            data=DataRefresh(),
            freezes=BlockingBothFreezes(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncFreezeObserver((), capacity=2, thread_name="test-freeze-progress-observer"),
            sealing=Sealing(index),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=published.append,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )
    first = CycleRequest(
        Strategy.TOMORROW,
        observed_at.date(),
        observed_at,
        "afternoon",
        1,
        "input-initial",
        False,
        observed_at,
    )
    second = replace(
        first,
        observed_at=observed_at + timedelta(seconds=10),
        sequence=2,
        input_version="input-latest",
    )
    runtime.start()
    runtime.submit_cycle(first)
    assert first_entered.wait(timeout=1.0)
    runtime.submit_cycle(second)
    release_first.set()

    try:
        assert second_entered.wait(timeout=1.0)
        current = index.snapshot(Strategy.TOMORROW).current
        assert isinstance(current, ScoredFreeze)
        assert current.sequence == 1
        clock.current = freeze_at
        runtime._dispatch_pipeline_task(  # noqa: SLF001 - verifies the score-to-freeze race boundary
            ScheduledPipelineTask(
                PipelineTask.FREEZE,
                freeze_at,
                MarketPhase.AFTER_CLOSE,
                (Strategy.TOMORROW.value,),
                SchedulePoint.AFTERNOON_FREEZE,
            )
        )
        assert _wait_for(lambda: index.snapshot(Strategy.TOMORROW).formal is not None)
        release_second.set()
        assert runtime.wait_idle(2.0)
    finally:
        release_first.set()
        release_second.set()
        runtime.stop(ShutdownDeadline.start(2.0))

    snapshot = index.snapshot(Strategy.TOMORROW)
    assert snapshot.formal is not None
    assert snapshot.formal.freeze.sequence == 1
    assert [event.projection.sequence for event in published if event.projection is not None] == [1]
    assert runtime.status().publish_rejection_count == 1


def test_direct_freeze_stream_delivery_survives_a_full_audit_observer_queue() -> None:
    audit_entered = threading.Event()
    audit_release = threading.Event()

    def blocked_audit(_observation) -> None:
        audit_entered.set()
        audit_release.wait(timeout=2.0)

    events = []
    index = UnifiedFreezeIndex()
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=FixedClock(NOW),
            calendar=TradingCalendar(),
            cadence=_cadence(NOW),
            data=DataRefresh(),
            freezes=Freezes(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncFreezeObserver(
                (blocked_audit,),
                capacity=1,
                thread_name="test-full-audit-observer",
            ),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=events.append,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )
    runtime.start()
    try:
        for sequence in (1, 3, 5):
            runtime.submit_cycle(
                CycleRequest(
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
                    isinstance(index.snapshot(Strategy.TOMORROW).current, ScoredFreeze)
                    and index.snapshot(Strategy.TOMORROW).current.sequence == expected
                )
            )
        assert audit_entered.wait(timeout=1.0)
        assert len(events) == 3
        assert runtime.status().observer_rejection_count >= 1
    finally:
        audit_release.set()
        runtime.stop(ShutdownDeadline.start(2.0))


def test_refresh_failure_retains_last_valid_freeze_without_cascading_build_failure() -> None:
    class FailingData(DataRefresh):
        fail = True

        def refresh(self, request: CycleRequest) -> None:
            super().refresh(request)
            if self.fail:
                raise DataRefreshUnavailableError("source unavailable")

    class TrackingFreezes(Freezes):
        def __init__(self) -> None:
            self.calls: list[Strategy] = []

        def build_local(self, request: CycleRequest):
            self.calls.append(request.strategy)
            return super().build_local(request)

    index = UnifiedFreezeIndex()
    previous = replace(freeze(Strategy.TOMORROW, sequence=1), observed_at=NOW - timedelta(minutes=1))
    assert index.publish(previous, expected_version=None).accepted
    freezes = TrackingFreezes()
    data = FailingData()
    clock = FixedClock(NOW)
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=clock,
            monotonic=clock.monotonic,
            calendar=TradingCalendar(),
            cadence=_cadence(NOW),
            data=data,
            freezes=freezes,
            reviews=SharedReviews(),
            index=index,
            observer=AsyncFreezeObserver((), capacity=1, thread_name="test-failure-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )
    request = CycleRequest(
        Strategy.TOMORROW,
        NOW.date(),
        NOW,
        "afternoon",
        3,
        "input-initial",
        True,
        NOW + timedelta(minutes=1),
    )
    runtime.start()
    runtime.submit_cycle(request)
    assert runtime.wait_idle(1.0)
    status = runtime.status()

    current = index.snapshot(Strategy.TOMORROW).current
    assert current is previous
    assert freezes.calls == []
    assert status.refresh_failure_count == 1
    assert status.freeze_failure_count == 0
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

    payload = RuntimeStatus(runtime, StatusReviewer(), lambda: {})  # type: ignore[arg-type]
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
    assert payload["runtime_version"] == "runtime-current"
    assert payload["scheduler"]["freeze_failure_count"] == 0  # type: ignore[index]
    assert payload["scheduler"]["cadence"]["started_at"] == NOW.isoformat()  # type: ignore[index]
    assert payload["scheduler"]["control"] == {  # type: ignore[index]
        "running": True,
        "inflight": 0,
        "rejected_count": 0,
    }
    assert payload["scheduler"]["freeze_completed_count"] == 0  # type: ignore[index]
    assert payload["scheduler"]["seal_failure_count"] == 0  # type: ignore[index]

    data.fail = False
    clock.current = NOW + timedelta(minutes=1)
    runtime.submit_cycle(replace(request, sequence=5, input_version="input-latest", allow_review=False))
    assert runtime.wait_idle(1.0)
    recovered = runtime.status()
    runtime.stop(ShutdownDeadline.start(1.0))

    assert recovered.strategy_error_codes == ()
    assert recovered.last_error_code == ""
    assert recovered.recent_errors[0].recovery_status == "recovered"
    assert recovered.recent_errors[0].resolved_at == NOW + timedelta(minutes=1)


def test_successful_data_task_recovers_only_its_own_refresh_issue() -> None:
    class FailingTasks(DataRefresh):
        failing_tasks = {PipelineTask.FULL_MARKET, PipelineTask.CANDIDATE_QUOTES}
        pending = True

        def refresh_task(self, request) -> RefreshOutcome:
            if self.pending:
                raise DataRefreshPendingError("history_projection_loading")
            if request.task in self.failing_tasks:
                raise DataRefreshUnavailableError("history_projection_loading")
            return super().refresh_task(request)

    class StatusReviewer:
        def status(self):
            return {"status": "ready", "budget": {"limit": 168, "used": 0, "remaining": 168}}

    data = FailingTasks()
    clock = FixedClock(NOW)
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-task-issue-control",
            ),
            clock=clock,
            calendar=TradingCalendar(),
            cadence=_cadence(NOW),
            data=data,
            freezes=Freezes(),
            reviews=SharedReviews(),
            index=UnifiedFreezeIndex(),
            observer=AsyncFreezeObserver((), capacity=1, thread_name="test-task-issue-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )
    full_market = ScheduledPipelineTask(PipelineTask.FULL_MARKET, NOW, MarketPhase.BEFORE_CLOSE)
    candidate_quotes = ScheduledPipelineTask(PipelineTask.CANDIDATE_QUOTES, NOW, MarketPhase.BEFORE_CLOSE)
    runtime.start()
    try:
        runtime._process_pipeline_task(full_market)
        assert runtime.status().refresh_failure_count == 0
        assert runtime.status().recent_errors == ()
        data.pending = False
        runtime._process_pipeline_task(full_market)
        runtime._process_pipeline_task(full_market)
        runtime._process_pipeline_task(candidate_quotes)
        failed = runtime.status()
        assert failed.refresh_failure_count == 3
        assert len(failed.recent_errors) == 2
        assert next(issue for issue in failed.recent_errors if issue.task is PipelineTask.FULL_MARKET).count == 2

        data.failing_tasks = {PipelineTask.CANDIDATE_QUOTES}
        clock.current = NOW + timedelta(minutes=1)
        runtime._process_pipeline_task(full_market)
        assert runtime.wait_idle(1.0)
        recovered = runtime.status()
        issues = {issue.task: issue for issue in recovered.recent_errors}
        assert issues[PipelineTask.FULL_MARKET].recovery_status == "recovered"
        assert issues[PipelineTask.FULL_MARKET].resolved_at == clock.current
        assert issues[PipelineTask.CANDIDATE_QUOTES].recovery_status == "active"

        payload = RuntimeStatus(runtime, StatusReviewer(), lambda: {})  # type: ignore[arg-type]
        assert payload["health"]["issue_count"] == 1
        assert sum(issue["recovery_status"] == "active" for issue in payload["recent_errors"]) == 1

        data.failing_tasks = set()
        clock.current = NOW + timedelta(minutes=2)
        runtime._process_pipeline_task(candidate_quotes)
        payload = RuntimeStatus(runtime, StatusReviewer(), lambda: {})  # type: ignore[arg-type]
        assert payload["health"]["issue_count"] == 0
        assert all(issue["recovery_status"] == "recovered" for issue in payload["recent_errors"])
    finally:
        runtime.stop(ShutdownDeadline.start(2.0))


def test_successful_hybrid_publish_recovers_freeze_path_issues() -> None:
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=FixedClock(NOW),
            calendar=TradingCalendar(),
            cadence=_cadence(NOW),
            data=DataRefresh(),
            freezes=Freezes(),
            reviews=SharedReviews(),
            index=UnifiedFreezeIndex(),
            observer=AsyncFreezeObserver((), capacity=1, thread_name="test-hybrid-recovery-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )
    runtime._record_failure("review", "review_unavailable", Strategy.TOMORROW)

    runtime._record_publish(hybrid=True, event=None, identity=freeze(Strategy.TOMORROW))
    status = runtime.status()
    runtime.stop(ShutdownDeadline.start(1.0))

    assert status.hybrid_publish_count == 1
    assert status.strategy_error_codes == ()
    assert status.last_error_code == ""
    assert status.recent_errors[0].recovery_status == "recovered"
    assert status.recent_errors[0].resolved_at == NOW


def test_runtime_error_history_is_bounded_and_repeated_failures_are_coalesced() -> None:
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=FixedClock(NOW),
            calendar=TradingCalendar(),
            cadence=_cadence(NOW),
            data=DataRefresh(),
            freezes=Freezes(),
            reviews=SharedReviews(),
            index=UnifiedFreezeIndex(),
            observer=AsyncFreezeObserver((), capacity=1, thread_name="test-error-history-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
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
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=FixedClock(NOW),
            calendar=TradingCalendar(),
            cadence=_cadence(NOW),
            data=DataRefresh(),
            freezes=Freezes(),
            reviews=SharedReviews(),
            index=UnifiedFreezeIndex(),
            observer=AsyncFreezeObserver((), capacity=1, thread_name="test-freeze-issue-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )
    runtime._record_failure("seal", "freeze_unavailable", Strategy.TOMORROW)
    runtime._record_failure("freeze", "freeze_unavailable", Strategy.TOMORROW)
    assert runtime.status().freeze_failure_count == 1
    assert runtime.status().seal_failure_count == 1
    request = CycleRequest(
        Strategy.TOMORROW,
        NOW.date(),
        NOW,
        "afternoon",
        1,
        "input-initial",
        False,
        NOW + timedelta(minutes=1),
    )

    runtime.start()
    runtime.submit_cycle(request)
    assert runtime.wait_idle(1.0)
    status = runtime.status()
    runtime.stop(ShutdownDeadline.start(1.0))

    seal_issue = next(issue for issue in status.recent_errors if issue.stage == "seal")
    assert seal_issue.recovery_status == "active"
    assert seal_issue.severity == "error"
    assert status.strategy_error_codes == (("tomorrow", "seal:freeze_unavailable"),)


def test_afternoon_schedule_keeps_tomorrow_d25_and_long_running() -> None:
    afternoon = datetime(2026, 8, 11, 13, 30, tzinfo=SHANGHAI)
    data = DataRefresh()
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=FixedClock(afternoon),
            calendar=TradingCalendar(),
            cadence=_cadence(afternoon),
            data=data,
            freezes=Freezes(),
            reviews=SharedReviews(),
            index=UnifiedFreezeIndex(),
            observer=AsyncFreezeObserver((), capacity=1, thread_name="test-afternoon-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
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
    before_freeze = datetime(2026, 8, 11, 14, 59, 59, tzinfo=SHANGHAI)
    after_freeze = datetime(2026, 8, 11, 15, 0, 1, tzinfo=SHANGHAI)
    index = UnifiedFreezeIndex()
    fixture = freeze(Strategy.TOMORROW, sequence=1)
    fixture_quote = fixture.items[0].quote
    assert fixture_quote is not None
    current = replace(
        fixture,
        observed_at=before_freeze,
        items=(replace(fixture.items[0], quote=replace(fixture_quote, source_time=before_freeze)),),
    )
    assert index.publish(current, expected_version=None).accepted
    assert index.seal_for_freeze(
        Strategy.TOMORROW, boundary_at=before_freeze.replace(second=0) + timedelta(minutes=1)
    ).accepted
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=FixedClock(after_freeze),
            calendar=TradingCalendar(),
            cadence=_cadence(after_freeze),
            data=DataRefresh(),
            freezes=Freezes(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncFreezeObserver((), capacity=1, thread_name="test-late-tomorrow-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )
    request = CycleRequest(
        Strategy.TOMORROW,
        after_freeze.date(),
        after_freeze,
        "midday",
        3,
        "input-latest",
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
    index = UnifiedFreezeIndex()
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2,
                urgent_worker_count=1,
                queue_capacity=4,
                thread_name_prefix="test-control",
            ),
            clock=FixedClock(NOW),
            calendar=TradingCalendar(),
            cadence=_cadence(NOW),
            data=DataRefresh(),
            freezes=Freezes(),
            reviews=reviews,
            index=index,
            observer=AsyncFreezeObserver((), capacity=1, thread_name="test-deadline-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )
    request = CycleRequest(Strategy.TOMORROW, NOW.date(), NOW, "afternoon", 1, "input-initial", True, NOW)
    runtime.start()
    runtime.submit_cycle(request)
    assert runtime.wait_idle(1.0)
    status = runtime.status()
    runtime.stop(ShutdownDeadline.start(1.0))

    current = index.snapshot(Strategy.TOMORROW).current
    assert isinstance(current, ScoredFreeze)
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
