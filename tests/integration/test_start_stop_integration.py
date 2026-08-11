"""Joint lifecycle integration across session, cadence and process shutdown."""

from __future__ import annotations

from unittest.mock import Mock

from tests.pipeline_factory import build_pipeline
from trader.application.cadence import CadencePolicy, PipelineTask, SchedulePointLifecycle
from trader.application.events import InMemoryEventLedger
from trader.application.ports.snapshots import RecoverySummary
from trader.application.publisher import SnapshotPublisher
from trader.application.recommendations import RecommendationEngine
from trader.application.schedule import SHANGHAI, SchedulePoint
from trader.application.shutdown import ShutdownDeadline, ShutdownReport, ShutdownStep
from trader.application.status import RuntimeState
from trader.bootstrap import ApplicationSystem
from trader.domain.recommendation.models import Strategy


class OfflineCalendar:
    @staticmethod
    def is_trading_day(_day) -> bool:
        raise OSError("calendar unavailable")


class EmptyRepository:
    @staticmethod
    def initialize() -> None:
        return None

    @staticmethod
    def recover() -> RecoverySummary:
        return RecoverySummary()

    @staticmethod
    def enforce_retention() -> int:
        return 0

    @staticmethod
    def recommendation_dates(_strategy: Strategy) -> tuple[str, ...]:
        return ()

    @staticmethod
    def load_frozen(_strategy: Strategy, _trade_date: str):
        return None

    @staticmethod
    def load_live_overlay(_strategy: Strategy, _trade_date: str):
        return None


class UnusedMarket:
    @staticmethod
    def health() -> dict[str, object]:
        return {}


def test_calendar_failure_does_not_block_pipeline_initialization(recommendation_policy) -> None:
    now = _at("10:00:00")
    repository = EmptyRepository()
    pipeline = build_pipeline(
        UnusedMarket(),
        OfflineCalendar(),
        None,
        repository,
        InMemoryEventLedger(),
        SnapshotPublisher(history_size=4, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: now,
        cadence_policy=_policy(),
    )

    result = pipeline.initialize()

    assert result["catchup_frozen"] == 0
    session = pipeline.status()["dependencies"]["trading_session"]
    assert session["calendar_state"] == "calendar_unavailable"
    assert session["is_trading_day"] is None


def test_pipeline_rejects_old_generation_and_completes_schedule_point(recommendation_policy) -> None:
    started_at = _at("14:47:59")
    repository = EmptyRepository()
    state = RuntimeState()
    pipeline = build_pipeline(
        UnusedMarket(),
        Mock(is_trading_day=Mock(return_value=True)),
        None,
        repository,
        InMemoryEventLedger(),
        SnapshotPublisher(history_size=4, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        state,
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: started_at,
        cadence_policy=_policy(),
    )
    pipeline._refresh_trading_session(started_at)
    pipeline._cadence.plan(started_at, is_trading_day=True)
    scheduled = next(
        item
        for item in pipeline._cadence.plan(_at("14:48:00"), is_trading_day=True).tasks
        if item.task is PipelineTask.DEEPSEEK_CUTOFF
    )
    assert pipeline._submit_scheduled_task(scheduled)
    event = pipeline._queue.get()
    assert event is not None

    pipeline._process_event(event)

    assert (
        pipeline._cadence.schedule_point_lifecycle("2026-07-16", SchedulePoint.DEEPSEEK_CUTOFF)
        is SchedulePointLifecycle.COMPLETED
    )

    assert pipeline._submit_scheduled_task(scheduled) is False
    stale = pipeline._queue.get(timeout_seconds=0.01)
    assert stale is None
    generation = pipeline.session_status().generation
    pipeline._trading_session.rotate(started_at, reason="test_rotation")
    assert pipeline.session_status().generation == generation + 1
    pipeline._process_event(event)
    assert state.snapshot({})["counters"]["events_stale_session_rejected"] == 1


def test_application_system_uses_one_deadline_for_every_shutdown_resource() -> None:
    deadline = ShutdownDeadline(100.0, 30.0, monotonic=lambda: 100.0)
    completed = ShutdownStep("completed", True, False)
    supervisor = Mock()
    supervisor.stop.return_value = ShutdownReport.from_steps(deadline, (completed,))
    source_lanes = Mock()
    source_lanes.stop.return_value = (completed,)
    history_pool = Mock()
    history_pool.stop.return_value = completed
    research_pool = Mock()
    research_pool.stop.return_value = completed
    tomorrow_runtime = Mock()
    tomorrow_runtime.stop.return_value = completed
    market_cache = Mock()
    market_cache.stop.return_value = completed
    system = ApplicationSystem(
        settings=Mock(),
        strategy=Mock(),
        watchlist=Mock(),
        app=Mock(),
        supervisor=supervisor,
        pipeline=Mock(),
        repository=Mock(),
        publisher=Mock(),
        published_snapshots=Mock(),
        state=Mock(),
        market_cache=market_cache,
        history_pool=history_pool,
        research_pool=research_pool,
        source_lanes=source_lanes,
        tomorrow_v2_runtime=tomorrow_runtime,
    )

    report = system.stop(deadline=deadline)

    assert report.completed is True
    assert supervisor.stop.call_args.args == (deadline,)
    assert all(call.kwargs["deadline"] is deadline for call in source_lanes.stop.call_args_list)
    assert history_pool.stop.call_args.kwargs["deadline"] is deadline
    assert research_pool.stop.call_args.kwargs["deadline"] is deadline
    assert all(call.kwargs["deadline"] is deadline for call in tomorrow_runtime.stop.call_args_list)
    assert market_cache.stop.call_args.kwargs["deadline"] is deadline


def _at(clock: str):
    from datetime import datetime

    return datetime.fromisoformat(f"2026-07-16T{clock}").replace(tzinfo=SHANGHAI)


def _policy() -> CadencePolicy:
    return CadencePolicy.from_seconds(
        {
            "full_market": {"final_review": 30},
            "candidate_quotes": {"final_review": 5},
            "topk_quotes": {"final_review": 3},
            "long_quotes": {"final_review": 3},
            "score": {"final_review": 10},
            "industry_heat": {"final_review": 60},
            "market_news": {"final_review": 60},
            "stock_risk": {"final_review": 180},
        }
    )
