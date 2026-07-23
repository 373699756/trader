from __future__ import annotations

import json
import math
import queue
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from statistics import quantiles
from zoneinfo import ZoneInfo

from trader.application.events import EventAuditRecord
from trader.application.published_snapshots import PublishedSnapshotIndex
from trader.application.publisher import SnapshotPublisher, encode_sse
from trader.application.queries import RecommendationQueries
from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.models import (
    FusionMode,
    LiveOverlay,
    Recommendation,
    RecommendationAction,
    RecommendationSnapshot,
    ScoreBreakdown,
    Strategy,
)
from trader.web import create_app

NOW = datetime(2026, 7, 23, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class _Archive:
    def latest(self, strategy: Strategy) -> RecommendationSnapshot | None:
        return None

    def recommendation_dates(self, strategy: Strategy) -> Sequence[str]:
        return ()

    def load_frozen(self, strategy: Strategy, trade_date: str) -> RecommendationSnapshot | None:
        return None

    def load_live_overlay(self, strategy: Strategy, trade_date: str) -> LiveOverlay | None:
        return None

    def list_events(self, *, cursor: int, limit: int) -> Sequence[EventAuditRecord]:
        return ()


def _recommendation(feature: FeatureSnapshot, rank: int, *, strategy: Strategy) -> Recommendation:
    action = RecommendationAction.EXECUTABLE if rank <= 10 else RecommendationAction.OBSERVE
    return Recommendation(
        strategy=strategy,
        features=feature,
        score=ScoreBreakdown(
            {},
            82.0 - rank / 10,
            0.0,
            82.0 - rank / 10,
            None,
            0.0,
            0.0,
            82.0 - rank / 10,
            FusionMode.LOCAL_DEGRADED,
            False,
        ),
        local_risk_facts=(),
        deepseek_risk_facts=(),
        review=None,
        action=action,
        action_reason="threshold_met" if action is RecommendationAction.EXECUTABLE else "near_threshold",
        veto=False,
        rank=rank,
    )


def _snapshot(
    snapshot_id: str,
    application_feature_factory,
    *,
    changed_price: float | None = None,
    strategy: Strategy = Strategy.TODAY,
) -> RecommendationSnapshot:
    recommendations = tuple(
        _recommendation(
            application_feature_factory(f"600{index:03d}", NOW, industry=f"行业{index:02d}"),
            index,
            strategy=strategy,
        )
        for index in range(1, 19)
    )
    if changed_price is not None:
        first = recommendations[0]
        changed_quote = replace(
            first.features.quote,
            price=changed_price,
            data_version=f"quote:{snapshot_id}",
        )
        recommendations = (
            replace(first, features=replace(first.features, quote=changed_quote)),
            *recommendations[1:],
        )
    return RecommendationSnapshot(
        snapshot_id=snapshot_id,
        strategy=strategy,
        trade_date=NOW.date().isoformat(),
        phase="today_main",
        data_version=f"data:{snapshot_id}",
        strategy_version="strategy-v17",
        fusion_version="fusion-v2",
        fusion_mode=FusionMode.LOCAL_DEGRADED,
        published_at=NOW,
        recommendations=recommendations,
        filtered_count=342,
        filter_reasons={"hard_filter": 342},
        config_version="runtime-v17",
    )


def _p95_ms(samples: Sequence[float]) -> float:
    return round(quantiles(samples, n=100, method="inclusive")[94], 6)


def _measure_ms(operation: Callable[[], object], *, rounds: int = 100) -> float:
    for _ in range(10):
        operation()
    samples: list[float] = []
    for _ in range(rounds):
        started = time.perf_counter_ns()
        operation()
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    return _p95_ms(samples)


def _publish_resident_triplet(
    index: PublishedSnapshotIndex,
    application_feature_factory,
    resident_date: str,
) -> None:
    for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25):
        resident = replace(
            _snapshot(f"{strategy.value}-resident", application_feature_factory, strategy=strategy),
            trade_date=resident_date,
            frozen=True,
            phase="frozen",
        )
        index.publish(resident)


def test_d4_p6_sse_api_and_transfer_budgets(application_feature_factory) -> None:
    archive = _Archive()
    index = PublishedSnapshotIndex(archive)
    publisher = SnapshotPublisher(history_size=256, client_queue_size=2, now=lambda: NOW)
    base = _snapshot("today-base", application_feature_factory)
    index.publish(base)
    publisher.publish(base)
    resident_date = (NOW.date() - timedelta(days=1)).isoformat()
    _publish_resident_triplet(index, application_feature_factory, resident_date)
    subscription = publisher.open_subscription(publisher.last_sequence())
    enqueue_samples: list[float] = []
    last_event = None
    try:
        for tick in range(120):
            snapshot = _snapshot(
                f"today-{tick:03d}",
                application_feature_factory,
                changed_price=12.01 + tick / 1000,
            )
            started = time.perf_counter_ns()
            index.publish(snapshot)
            last_event = publisher.publish(snapshot)
            queued = subscription.queue.get_nowait()
            enqueue_samples.append((time.perf_counter_ns() - started) / 1_000_000)
            assert queued.sequence == last_event.sequence
    except queue.Empty as exc:  # pragma: no cover - produces the useful gate failure
        raise AssertionError("P6 publication did not enqueue an SSE event") from exc
    finally:
        publisher.unsubscribe(subscription.queue)

    queries = RecommendationQueries(index, archive, now=lambda: NOW)
    app = create_app(
        lambda: {"schema_version": "v3", "status": "running", "runtime_started": True},
        queries=queries,
        publisher=publisher,
    )
    client = app.test_client()
    current_path = "/api/recommendations/today?view=current&top_n=18"
    resident_path = f"/api/recommendations/today?date={resident_date}&top_n=18"
    current = client.get(current_path)
    etag = current.headers["ETag"]
    metrics = {
        "p6_to_sse_enqueue_p95_ms": _p95_ms(enqueue_samples),
        "current_api_p95_ms": _measure_ms(lambda: client.get(current_path)),
        "resident_api_p95_ms": _measure_ms(lambda: client.get(resident_path)),
        "etag_304_p95_ms": _measure_ms(lambda: client.get(current_path, headers={"If-None-Match": etag})),
        "dates_api_p95_ms": _measure_ms(lambda: client.get("/api/recommendation-dates?strategy=today")),
        "status_api_p95_ms": _measure_ms(lambda: client.get("/api/status")),
    }
    assert last_event is not None
    patch_bytes = len(encode_sse(last_event).encode("utf-8"))
    full_bytes = len(current.data)
    savings = round((1.0 - patch_bytes / full_bytes) * 100.0, 3)
    status = publisher.status()
    publish_latency = status["sse_publish_latency"]
    assert isinstance(publish_latency, Mapping)

    assert metrics["p6_to_sse_enqueue_p95_ms"] <= 100.0
    assert float(publish_latency["p95_seconds"]) <= 2.0
    assert metrics["current_api_p95_ms"] <= 200.0
    assert metrics["resident_api_p95_ms"] <= 200.0
    assert metrics["etag_304_p95_ms"] <= 50.0
    assert metrics["dates_api_p95_ms"] <= 100.0
    assert metrics["status_api_p95_ms"] <= 100.0
    assert patch_bytes < full_bytes
    assert savings > 50.0
    assert all(math.isfinite(value) for value in metrics.values())

    print(
        json.dumps(
            {
                **metrics,
                "authoritative_sse_p95_seconds": publish_latency["p95_seconds"],
                "full_response_bytes": full_bytes,
                "incremental_sse_bytes": patch_bytes,
                "transmission_savings_percent": savings,
            },
            sort_keys=True,
        )
    )
