import threading
import time
from unittest.mock import Mock, patch

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
    writer = Mock()
    target = AsyncSnapshotWriter(path, save_snapshot=writer, is_frozen=lambda: False)

    target.schedule({"ok": True})
    assert _wait_until(lambda: target.stats()["running"] is False and target.stats()["success_count"] >= 1)
    success_stats = target.stats()
    assert success_stats["success_count"] >= 1
    assert success_stats["last_error"] == ""

    writer.side_effect = RuntimeError("io failure")
    with patch("stock_analyzer.snapshot_writer._LOGGER.exception") as logger_call:
        target.schedule({"ok": False})
        assert _wait_until(lambda: target.stats()["running"] is False and target.stats()["failure_count"] >= 1)
        failure_stats = target.stats()
        assert failure_stats["failure_count"] >= 1
        assert "io failure" in str(failure_stats["last_error"])
        assert logger_call.called


def test_async_snapshot_writer_writes_when_frozen_and_snapshot_missing(tmp_path):
    path = str(tmp_path / "snapshot.json")
    writer = Mock()
    target = AsyncSnapshotWriter(
        path,
        save_snapshot=writer,
        is_frozen=lambda: True,
        path_exists=lambda _path: False,
    )

    target.schedule({"ok": True})
    assert _wait_until(lambda: target.stats()["running"] is False and target.stats()["success_count"] >= 1)
    assert writer.called


def test_async_snapshot_writer_skips_when_frozen_and_snapshot_exists(tmp_path):
    path = str(tmp_path / "snapshot.json")
    writer = Mock()
    target = AsyncSnapshotWriter(
        path,
        save_snapshot=writer,
        load_snapshot=Mock(return_value={"ok": True, "status": "ok"}),
        is_frozen=lambda: True,
        path_exists=lambda _path: True,
    )

    target.schedule({"ok": True})
    assert not target.stats()["running"]
    assert not writer.called


def test_async_snapshot_writer_rewrites_when_frozen_snapshot_invalid(tmp_path):
    path = str(tmp_path / "snapshot.json")
    writer = Mock()
    target = AsyncSnapshotWriter(
        path,
        save_snapshot=writer,
        load_snapshot=Mock(return_value={"ok": False, "status": "invalid"}),
        is_frozen=lambda: True,
        path_exists=lambda _path: True,
    )

    target.schedule({"ok": True})
    assert _wait_until(lambda: target.stats()["running"] is False and target.stats()["success_count"] >= 1)
    assert writer.called


def test_async_snapshot_writer_falls_back_to_write_when_snapshot_check_fails(tmp_path):
    path = str(tmp_path / "snapshot.json")
    writer = Mock()
    target = AsyncSnapshotWriter(
        path,
        save_snapshot=writer,
        is_frozen=lambda: True,
        path_exists=Mock(side_effect=OSError("fs check fail")),
    )

    target.schedule({"ok": True})
    assert _wait_until(lambda: target.stats()["running"] is False and target.stats()["success_count"] >= 1)
    assert writer.called


def test_async_snapshot_writer_takes_payload_ownership_before_background_write(tmp_path):
    started = threading.Event()
    release = threading.Event()
    saved_payloads = []

    def save_snapshot(_path, payload):
        started.set()
        assert release.wait(1.0)
        saved_payloads.append(payload)

    target = AsyncSnapshotWriter(
        str(tmp_path / "snapshot.json"),
        save_snapshot=save_snapshot,
        is_frozen=lambda: False,
    )
    payload = {"rows": [{"code": "600001"}]}

    target.schedule(payload)
    assert started.wait(1.0)
    payload["rows"][0]["code"] = "changed"
    release.set()
    target.stop(1.0)

    assert saved_payloads == [{"rows": [{"code": "600001"}]}]


def test_async_snapshot_writer_recovers_after_thread_start_failure(tmp_path):
    calls = 0

    class FailingThread:
        def start(self):
            raise RuntimeError("thread unavailable")

        def is_alive(self):
            return False

    def thread_factory(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return FailingThread()
        return threading.Thread(**kwargs)

    writer = Mock()
    target = AsyncSnapshotWriter(
        str(tmp_path / "snapshot.json"),
        save_snapshot=writer,
        is_frozen=lambda: False,
        thread_factory=thread_factory,
    )

    target.schedule({"attempt": 1})
    assert target.stats()["running"] is False
    assert target.stats()["failure_count"] == 1

    target.schedule({"attempt": 2})
    assert _wait_until(lambda: target.stats()["success_count"] == 1)
    assert writer.call_args.args[1] == {"attempt": 2}


def test_async_snapshot_writer_stop_waits_for_active_write(tmp_path):
    started = threading.Event()
    release = threading.Event()
    stopped = threading.Event()

    def save_snapshot(_path, _payload):
        started.set()
        assert release.wait(1.0)

    target = AsyncSnapshotWriter(
        str(tmp_path / "snapshot.json"),
        save_snapshot=save_snapshot,
        is_frozen=lambda: False,
    )
    target.schedule({"ok": True})
    assert started.wait(1.0)

    stop_thread = threading.Thread(target=lambda: (target.stop(1.0), stopped.set()))
    stop_thread.start()
    assert not stopped.wait(0.05)
    assert target.stats()["stopping"] is True

    release.set()
    assert stopped.wait(1.0)
    stop_thread.join(1.0)
    assert target.stats()["running"] is False
    assert target.stats()["stopping"] is False
