from __future__ import annotations

import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.infra.market_data.history.history import DailyBar, PriceAdjustment
from trader.recommendation.infra.market_data.history_recovery import HistoryRecovery
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache
from trader.recommendation.application.runtime.workers import BoundedExecutor


NOW = datetime(2026, 9, 23, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _bars(count: int, *, adjustment: PriceAdjustment = PriceAdjustment.QFQ) -> tuple[DailyBar, ...]:
    return tuple(
        DailyBar(
            trade_date=(NOW.date() - timedelta(days=count - index)).isoformat(),
            open_price=10.0,
            close=10.0,
            high=10.5,
            low=9.5,
            volume=1_000.0,
            amount=100_000_000.0,
            pct_change=0.0,
            adjustment=adjustment,
            source="fixture",
        )
        for index in range(count)
    )


class _Source:
    def __init__(self, values: dict[str, tuple[DailyBar, ...]]) -> None:
        self.values = values
        self.calls: list[str] = []

    def fetch_history(self, code: str, *, days: int = 90) -> tuple[DailyBar, ...]:
        self.calls.append(code)
        return self.values.get(code, ())[-days:]


class _UnavailableArchive:
    def manifest(self):
        return None


def test_recovery_prefers_primary_and_returns_full_qfq_window() -> None:
    primary = _Source({"600001": _bars(61)})
    fallback = _Source({"600001": _bars(80)})
    recovery = HistoryRecovery(primary, fallback, worker_pool=None, workers=2, wall_clock=lambda: NOW)

    result = recovery.recover(("600001",), days=61, deadline=NOW + timedelta(seconds=5))

    assert len(result["600001"]) == 61
    assert primary.calls == ["600001"]
    assert fallback.calls == []
    assert recovery.status().success_count == 1


def test_recovery_falls_back_when_primary_has_insufficient_history() -> None:
    primary = _Source({"600001": _bars(19)})
    fallback = _Source({"600001": _bars(61)})
    recovery = HistoryRecovery(primary, fallback, worker_pool=None, workers=2, wall_clock=lambda: NOW)

    result = recovery.recover(("600001",), days=61, deadline=NOW + timedelta(seconds=5))

    assert len(result["600001"]) == 61
    assert primary.calls == ["600001"]
    assert fallback.calls == ["600001"]


def test_recovery_rejects_raw_bars_and_does_not_fabricate_history() -> None:
    primary = _Source({"600001": _bars(61, adjustment=PriceAdjustment.RAW)})
    fallback = _Source({"600001": ()})
    recovery = HistoryRecovery(primary, fallback, worker_pool=None, workers=1, wall_clock=lambda: NOW)

    result = recovery.recover(("600001",), days=61, deadline=NOW + timedelta(seconds=5))

    assert result == {}
    assert recovery.status().failure_count == 1


def test_published_history_uses_recovery_when_archive_has_no_snapshot() -> None:
    primary = _Source({"600001": _bars(61)})
    recovery = HistoryRecovery(primary, _Source({}), worker_pool=None, workers=1, wall_clock=lambda: NOW)
    history = PublishedHistoryCache(_UnavailableArchive(), lookback_sessions=61, recovery=recovery)
    restrictions: dict[str, set[str]] = {}

    loaded = history.load(("600001",), deadline=NOW + timedelta(seconds=5), action_restrictions=restrictions)

    assert len(loaded["600001"]) == 61
    assert restrictions == {}
    assert history.summaries(loaded, NOW)["600001"].profile.median_amount_20d == 100_000_000.0


@pytest.mark.parametrize("succeeds", (False, True))
def test_recovery_rotates_failures_and_expired_successes_before_retrying(succeeds: bool) -> None:
    codes = ("600001", "300001", "688001", "600002", "300002")
    primary = _Source({code: _bars(61) for code in codes} if succeeds else {})
    monotonic = [0.0]
    recovery = HistoryRecovery(
        primary,
        _Source({}),
        worker_pool=None,
        workers=1,
        max_batch_size=2,
        wall_clock=lambda: NOW,
        monotonic_clock=lambda: monotonic[0],
        ttl_seconds=1,
    )
    history = PublishedHistoryCache(_UnavailableArchive(), lookback_sessions=61, recovery=recovery)
    for round_index in range(3):
        restrictions: dict[str, set[str]] = {}
        loaded = history.load(tuple(reversed(codes)) if round_index % 2 else codes, action_restrictions=restrictions)
        assert all(reason == {"history_data_pending"} for reason in restrictions.values())
        assert set(restrictions) == set(codes) - set(loaded)
        status = history.status().recovery
        assert status.requested_count == 5
        assert status.dispatched_count == 2
        assert status.deferred_count == 3
        assert status.success_count + status.failure_count == status.dispatched_count
        monotonic[0] += 2
    # Even if earlier successes expire or failures persist, all issuers get a
    # turn before any repeats, independently of the input ordering.
    assert len(primary.calls[:5]) == len(set(primary.calls[:5])) == 5
    assert set(primary.calls[:5]) == set(codes)


def test_cache_hits_do_not_launch_workers_or_extend_ttl() -> None:
    primary = _Source({"600001": _bars(61)})
    monotonic = [0.0]
    recovery = HistoryRecovery(
        primary,
        _Source({}),
        worker_pool=None,
        workers=1,
        wall_clock=lambda: NOW,
        monotonic_clock=lambda: monotonic[0],
        ttl_seconds=2,
    )
    first = recovery.recover(("600001",), days=61, deadline=None)
    monotonic[0] = 1
    assert recovery.recover(("600001",), days=61, deadline=None) == first
    assert recovery.status().cache_hit_count == 1
    assert recovery.status().dispatched_count == 0
    monotonic[0] = 2
    recovery.recover(("600001",), days=61, deadline=None)
    assert primary.calls == ["600001", "600001"]


def test_expired_deadline_never_requests_a_vendor_or_advances_rotation() -> None:
    primary = _Source({})
    recovery = HistoryRecovery(
        primary, _Source({}), worker_pool=None, workers=1, max_batch_size=1, wall_clock=lambda: NOW
    )
    assert recovery.recover(("600001", "600002"), days=61, deadline=NOW) == {}
    assert primary.calls == []
    assert recovery.status().dispatched_count == 0
    assert recovery.status().deferred_count == 2
    recovery.recover(("600001", "600002"), days=61, deadline=None)
    assert primary.calls == ["600001"]


def test_deadline_stops_fallback_and_preserves_priority_of_unstarted_waves() -> None:
    monotonic = [0.0]

    class _SlowSource(_Source):
        def fetch_history(self, code: str, *, days: int = 90) -> tuple[DailyBar, ...]:
            result = super().fetch_history(code, days=days)
            monotonic[0] += 2
            return result

    primary = _SlowSource({})
    fallback = _Source({})
    pool = BoundedExecutor(worker_count=1, queue_capacity=1, thread_name_prefix="test-history")
    pool.start()
    recovery = HistoryRecovery(
        primary,
        fallback,
        worker_pool=pool,
        workers=1,
        max_batch_size=3,
        batch_timeout_seconds=1,
        wall_clock=lambda: NOW,
        monotonic_clock=lambda: monotonic[0],
    )
    try:
        # Run on the owned worker to exercise the production nested-inline path.
        future = pool.submit(recovery.recover, ("600001", "600002", "600003"), days=61, deadline=None)
        assert future is not None
        assert future.result(timeout=2) == {}
        assert primary.calls == ["600001"]
        assert fallback.calls == []
        assert recovery.status().timeout_count == 1
        assert recovery.status().deferred_count == 2
        future = pool.submit(recovery.recover, ("600001", "600002", "600003"), days=61, deadline=None)
        assert future is not None
        future.result(timeout=2)
        assert primary.calls == ["600001", "600002"]
    finally:
        pool.stop(wait=True, cancel_futures=True)


def test_timed_out_inflight_code_is_not_duplicated_or_published_late() -> None:
    release = threading.Event()

    class _BlockingSource(_Source):
        def fetch_history(self, code: str, *, days: int = 90) -> tuple[DailyBar, ...]:
            if code == "600001":
                assert release.wait(2)
            return super().fetch_history(code, days=days)

    primary = _BlockingSource({code: _bars(61) for code in ("600001", "600002")})
    pool = BoundedExecutor(worker_count=2, queue_capacity=2, thread_name_prefix="test-history")
    pool.start()
    recovery = HistoryRecovery(
        primary,
        _Source({}),
        worker_pool=pool,
        workers=1,
        max_batch_size=1,
        batch_timeout_seconds=0.05,
        wall_clock=lambda: NOW,
    )
    try:
        assert recovery.recover(("600001", "600002"), days=61, deadline=None) == {}
        assert recovery.status().inflight_count == 1
        assert recovery.status().timeout_count == 1
        assert set(recovery.recover(("600001", "600002"), days=61, deadline=None)) == {"600002"}
        assert recovery.status().inflight_count == 1
    finally:
        release.set()
        pool.stop(wait=True)
    assert primary.calls.count("600001") == 1
    assert recovery.status().inflight_count == 0
    # The late result did not become a cache hit.
    recovery.recover(("600001",), days=61, deadline=None)
    assert primary.calls.count("600001") == 2
    assert recovery.status().cache_hit_count == 0
