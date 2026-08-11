from __future__ import annotations

import queue
import time
from collections.abc import Callable, Sequence
from datetime import date, datetime
from statistics import quantiles
from zoneinfo import ZoneInfo

from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.decision_events import build_v2_decision_committed
from trader.application.decision_queries import UnifiedDecisionQueries
from trader.application.decision_stream import UnifiedDecisionEventStream
from trader.domain.recommendation.decision_identity import DecisionItem, ScoredDecision
from trader.domain.recommendation.models import RecommendationAction, Strategy
from trader.web import create_app
from trader.web.route_services import UnifiedWebServices

NOW = datetime(2026, 8, 11, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


class _Clock:
    def now(self) -> datetime:
        return NOW


class _History:
    def load(self, strategy: Strategy, trade_date: date):
        return None

    def list_dates(self, strategy: Strategy, *, limit: int = 31) -> tuple[date, ...]:
        return (date(2026, 8, 8),)[:limit]


def test_unified_v2_sse_and_read_api_latency_budgets() -> None:
    index = UnifiedDecisionIndex()
    stream = UnifiedDecisionEventStream(history_size=256, client_queue_size=2)
    base = _decision(1)
    assert index.publish(base, expected_version=None).accepted
    stream.publish_committed(build_v2_decision_committed(base))
    subscription = stream.open_subscription(stream.last_sequence())
    enqueue_samples: list[float] = []
    current = base
    try:
        for sequence in range(2, 122):
            decision = _decision(sequence)
            started = time.perf_counter_ns()
            assert index.publish(decision, expected_version=current.version).accepted
            event = stream.publish_committed(build_v2_decision_committed(decision))
            queued = subscription.queue.get_nowait()
            enqueue_samples.append((time.perf_counter_ns() - started) / 1_000_000)
            assert queued.sequence == event.sequence
            current = decision
    except queue.Empty as exc:
        raise AssertionError("unified publication did not enqueue an SSE event") from exc
    finally:
        stream.unsubscribe(subscription.queue)

    queries = UnifiedDecisionQueries(index, _History(), _Clock())
    app = create_app(
        services=UnifiedWebServices(
            queries,
            stream,
            lambda: {"status": "running", "runtime_started": True},
        )
    )
    client = app.test_client()
    current_path = "/api/v2/decisions/today/current"
    initial = client.get(current_path)
    etag = initial.headers["ETag"]

    assert _p95_ms(enqueue_samples) <= 100.0
    assert _measure_ms(lambda: client.get(current_path)) <= 200.0
    assert _measure_ms(lambda: client.get(current_path, headers={"If-None-Match": etag})) <= 50.0
    assert _measure_ms(lambda: client.get("/api/v2/decisions/today/dates")) <= 100.0
    assert _measure_ms(lambda: client.get("/api/v2/status")) <= 100.0


def _decision(sequence: int, *, strategy: Strategy = Strategy.TODAY) -> ScoredDecision:
    items = tuple(
        DecisionItem(
            f"600{index:03d}",
            RecommendationAction.EXECUTABLE if index <= 6 else RecommendationAction.OBSERVE,
            True,
            index,
            90.0 - index,
            85.0 - index / 10,
            85.0 - index / 10,
            (("local_score", 85.0 - index / 10),),
            (),
            "threshold_met" if index <= 6 else "near_threshold",
        )
        for index in range(1, 13)
    )
    return ScoredDecision(
        strategy,
        NOW.date(),
        sequence,
        NOW,
        "local",
        None,
        (("market", f"market:{sequence}"),),
        "config:1",
        "strategy:1",
        "fusion:1",
        items,
        (("hard_filter", 342),),
    )


def _measure_ms(operation: Callable[[], object], *, rounds: int = 100) -> float:
    for _ in range(10):
        operation()
    samples: list[float] = []
    for _ in range(rounds):
        started = time.perf_counter_ns()
        operation()
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    return _p95_ms(samples)


def _p95_ms(samples: Sequence[float]) -> float:
    return round(quantiles(samples, n=100, method="inclusive")[94], 6)
