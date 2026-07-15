from unittest.mock import Mock

from stock_analyzer.app_container import PayloadCache
from stock_analyzer.services.recommendation_cache import (
    RecommendationRefreshService,
    RecommendationSnapshotService,
)


def _snapshot_service(
    recommendation_cache: PayloadCache,
    horizon_cache: PayloadCache,
    *,
    snapshot_loader=None,
) -> RecommendationSnapshotService:
    return RecommendationSnapshotService(
        recommendation_cache,
        horizon_cache,
        is_frozen=lambda: False,
        overlay_live_quotes=lambda payload: {**payload, "live_overlay": True},
        snapshot_path="snapshot.json",
        snapshot_max_age_seconds=60,
        snapshot_loader=snapshot_loader or Mock(return_value={"ok": False}),
    )


def test_snapshot_service_uses_explicit_cache_and_overlay_dependencies():
    recommendation_cache = PayloadCache(max_entries=4, ttl_seconds=60)
    horizon_cache = PayloadCache(max_entries=4, ttl_seconds=60)
    service = _snapshot_service(recommendation_cache, horizon_cache)

    entry = service.remember_recommendation_payload(
        5,
        "all",
        {"ok": True, "data": []},
        source="test",
    )
    served = service.serve_recommendation_payload(entry)

    assert served["ok"] is True
    assert served["live_overlay"] is True
    assert service.recommendation_cache is recommendation_cache
    assert service.horizon_cache is horizon_cache
    assert not hasattr(service, "context")


def test_snapshot_service_injects_snapshot_path_and_validation_contract():
    snapshot_loader = Mock(
        return_value={
            "ok": True,
            "payload": {"ok": True, "data": [{"code": "600001"}]},
            "saved_at": "2026-07-15T10:00:00",
            "age_seconds": 5,
        }
    )
    service = _snapshot_service(
        PayloadCache(max_entries=4, ttl_seconds=60),
        PayloadCache(max_entries=4, ttl_seconds=60),
        snapshot_loader=snapshot_loader,
    )

    entry = service.snapshot_entry(5, "all")

    assert entry is not None
    snapshot_loader.assert_called_once_with(
        "snapshot.json",
        max_age_seconds=60,
        expected_market="all",
        expected_top_n=5,
    )


def test_refresh_service_only_depends_on_declared_capabilities():
    recommendation_cache = PayloadCache(max_entries=4, ttl_seconds=60)
    snapshots = _snapshot_service(
        recommendation_cache,
        PayloadCache(max_entries=4, ttl_seconds=60),
    )
    refresh_quotes = Mock()
    build_recommendations = Mock(return_value=({"ok": True}, 200))
    service = RecommendationRefreshService(
        snapshots,
        refresh_quotes=refresh_quotes,
        build_recommendations=build_recommendations,
        build_horizon=Mock(return_value={"ok": True}),
        provider_health=Mock(return_value={}),
        research_disclaimer=Mock(return_value="research only"),
        is_frozen=lambda: False,
    )
    key = snapshots.recommendation_cache_key(5, "all")
    assert recommendation_cache.mark_refreshing(key)

    service.refresh_recommendation_cache(5, "all")

    refresh_quotes.assert_called_once_with()
    build_recommendations.assert_called_once_with(5, "all", include_deepseek=True)
    assert recommendation_cache.mark_refreshing(key)
    assert not hasattr(service, "context")


def test_refresh_service_keeps_new_workers_blocked_after_stop_timeout():
    class PendingThread:
        def __init__(self, **_kwargs):
            self.alive = True

        def start(self):
            return None

        def join(self, _timeout):
            return None

        def is_alive(self):
            return self.alive

    created_threads = []

    def thread_factory(**kwargs):
        worker = PendingThread(**kwargs)
        created_threads.append(worker)
        return worker

    recommendation_cache = PayloadCache(max_entries=4, ttl_seconds=60)
    snapshots = _snapshot_service(
        recommendation_cache,
        PayloadCache(max_entries=4, ttl_seconds=60),
    )
    service = RecommendationRefreshService(
        snapshots,
        refresh_quotes=Mock(),
        build_recommendations=Mock(return_value=({"ok": True}, 200)),
        build_horizon=Mock(return_value={"ok": True}),
        provider_health=Mock(return_value={}),
        research_disclaimer=Mock(return_value="research only"),
        is_frozen=lambda: False,
        thread_factory=thread_factory,
    )

    assert service.schedule_recommendation_refresh(5, "all")
    service.stop(timeout_seconds=0)

    assert service.status()["stopping"] is True
    assert not service.schedule_recommendation_refresh(6, "all")
    assert len(created_threads) == 1

    created_threads[0].alive = False
    service.stop(timeout_seconds=0)
    assert service.status()["stopping"] is False
