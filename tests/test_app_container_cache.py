import time
from unittest.mock import patch

import pandas as pd

from stock_analyzer.app_container import AsyncSnapshotWriter, PayloadCache
from stock_analyzer.providers import TimedCache


def test_timedcache_returns_copies_for_mutable_payloads():
    cache = TimedCache(60)
    source_df = pd.DataFrame([{"code": "600001", "price": 10.0}])
    cache.set({"codes": [1, 2], "frame": source_df})

    first = cache.get()
    first["codes"].append(3)
    first["frame"].loc[0, "code"] = "600002"

    second = cache.get()
    assert second is not None
    assert second["codes"] == [1, 2]
    assert second["frame"].iloc[0]["code"] == "600001"


def test_payload_cache_ttl_and_entry_eviction_ordered():
    cache = PayloadCache(max_entries=2, ttl_seconds=60)
    cache.remember(("a",), {"v": 1})
    cache.remember(("b",), {"v": 2})
    # Access a to make it recently used, keeping it from eviction.
    assert cache.get(("a",))["payload"]["v"] == 1

    cache.remember(("c",), {"v": 3})
    assert cache.get(("a",)) is not None
    assert cache.get(("b",)) is None
    assert cache.stats()["entries"] == 2
    assert cache.stats()["evictions"] >= 1


def test_payload_cache_ttl_expires_entries():
    cache = PayloadCache(max_entries=2, ttl_seconds=1)
    cache.remember(("a",), {"v": 1})
    time.sleep(1.2)
    assert cache.get(("a",)) is None
    assert cache.stats()["expired"] >= 1


def test_payload_cache_refresh_single_flight():
    cache = PayloadCache(max_entries=8, ttl_seconds=60)
    assert cache.mark_refreshing(("x",))
    assert not cache.mark_refreshing(("x",))
    cache.discard_refreshing(("x",))
    assert cache.mark_refreshing(("x",))


def _wait_until(predicate, timeout_seconds=1.0, sleep_seconds=0.01):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(sleep_seconds)
    return predicate()


def test_async_snapshot_writer_records_success_and_failure_metrics(tmp_path):
    path = str(tmp_path / "snapshot.json")
    target = AsyncSnapshotWriter(path)

    with patch("stock_analyzer.app_container.save_recommendation_snapshot"):
        target.schedule({"ok": True})
        assert _wait_until(lambda: target.stats()["running"] is False and target.stats()["success_count"] >= 1)
        success_stats = target.stats()
        assert success_stats["success_count"] >= 1
        assert success_stats["last_error"] == ""

    with patch("stock_analyzer.app_container.save_recommendation_snapshot", side_effect=RuntimeError("io failure")), patch(
        "stock_analyzer.app_container._LOGGER.exception"
    ) as logger_call:
        target.schedule({"ok": False})
        assert _wait_until(lambda: target.stats()["running"] is False and target.stats()["failure_count"] >= 1)
        failure_stats = target.stats()
        assert failure_stats["failure_count"] >= 1
        assert "io failure" in str(failure_stats["last_error"])
        assert logger_call.called
