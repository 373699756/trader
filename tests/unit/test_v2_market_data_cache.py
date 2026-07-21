from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NoReturn
from zoneinfo import ZoneInfo

from trader.application.cache import (
    CacheDatasetPolicy,
    CacheGroupPolicy,
    CachePolicy,
    build_cache_identity,
    canonical_json_bytes,
)
from trader.infrastructure.cache import BoundedLruCache

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI)


@dataclass
class MutableClocks:
    monotonic_value: float = 0.0
    wall_value: datetime = NOW

    def monotonic(self) -> float:
        return self.monotonic_value

    def wall(self) -> datetime:
        return self.wall_value

    def advance(self, seconds: float) -> None:
        self.monotonic_value += seconds
        self.wall_value += timedelta(seconds=seconds)


def _policy(*, capacity: int = 2, group_bytes: int = 100_000) -> CachePolicy:
    return CachePolicy(
        policy_version="cache_policy_v15",
        datasets={
            "full_market_quotes": CacheDatasetPolicy(
                refresh_ttl_seconds=None,
                action_max_age_seconds=None,
                cadence_task="full_market",
                action_max_age_multiplier=3.0,
                negative_ttl_seconds=10.0,
                capacity=capacity,
                group="realtime_observations",
                persisted=False,
            ),
            "daily_history": CacheDatasetPolicy(
                refresh_ttl_seconds=60.0,
                action_max_age_seconds=120.0,
                cadence_task=None,
                action_max_age_multiplier=None,
                negative_ttl_seconds=10.0,
                capacity=capacity,
                group="history_minutes_research",
                persisted=True,
            ),
        },
        groups={
            "realtime_observations": CacheGroupPolicy(max_bytes=group_bytes),
            "history_minutes_research": CacheGroupPolicy(max_bytes=group_bytes),
        },
        total_bytes=group_bytes * 2,
        estimator_version="canonical_json_utf8_v1",
    )


def _identity(subject: str, *, dataset: str = "daily_history", phase: str = "today_main"):
    return build_cache_identity(
        dataset=dataset,
        source="eastmoney",
        subject_key=subject,
        request={"codes": ["600002", "600001"], "fields": ["close"], "adjust": "qfq"},
        trade_date="2026-07-16",
        phase=phase,
        source_contract_version="eastmoney-v1",
        config_version="runtime-v15",
        schema_version="market-v15",
    )


def test_cache_identity_is_stable_and_slow_data_reuses_all_day_phase() -> None:
    first = _identity("600001", phase="today_main")
    second = build_cache_identity(
        dataset="daily_history",
        source="eastmoney",
        subject_key="600001",
        request={"adjust": "qfq", "fields": ["close"], "codes": ["600001", "600002"]},
        trade_date="2026-07-16",
        phase="final_review",
        source_contract_version="eastmoney-v1",
        config_version="runtime-v15",
        schema_version="market-v15",
    )

    assert first == second
    assert first.phase == "all_day"
    assert len(first.request_fingerprint) == 64


def test_cache_uses_monotonic_ttl_without_changing_business_source_time() -> None:
    clocks = MutableClocks()
    cache = BoundedLruCache(
        _policy(),
        cadence_seconds={"full_market": {"today_main": 10.0}},
        monotonic=clocks.monotonic,
        wall_clock=clocks.wall,
    )
    identity = _identity("market", dataset="full_market_quotes")
    source_time = NOW - timedelta(seconds=1)
    cache.put(identity, {"rows": 1}, data_version="quotes-v1", source_time=source_time)

    assert cache.get(identity).state == "fresh"
    clocks.advance(10.0)
    assert cache.get(identity).state == "fresh"
    clocks.advance(0.001)
    assert cache.get(identity).state == "refresh_due"
    clocks.advance(10.0)
    stale = cache.get(identity)
    assert stale.state == "stale"
    assert stale.source_time == source_time
    clocks.advance(10.0)
    assert cache.get(identity).state == "degraded"


def test_cache_never_marks_newly_inserted_but_old_source_data_as_fresh() -> None:
    clocks = MutableClocks()
    cache = BoundedLruCache(
        _policy(),
        cadence_seconds={"full_market": {"today_main": 10.0}},
        monotonic=clocks.monotonic,
        wall_clock=clocks.wall,
    )
    realtime = _identity("market", dataset="full_market_quotes")
    history = _identity("600001")

    cache.put(realtime, {"rows": 1}, data_version="quotes-old", source_time=NOW - timedelta(seconds=31))
    cache.put(history, {"close": 1.0}, data_version="history-old", source_time=NOW - timedelta(seconds=121))

    assert cache.get(realtime).state == "degraded"
    assert cache.get(history).state == "degraded"


def test_cache_actionability_uses_business_source_age_without_rewriting_it() -> None:
    clocks = MutableClocks()
    cache = BoundedLruCache(_policy(), monotonic=clocks.monotonic, wall_clock=clocks.wall)
    identity = _identity("600001")
    boundary = NOW - timedelta(seconds=120)

    assert cache.is_actionable(identity, boundary) is True
    assert cache.is_actionable(identity, boundary - timedelta(microseconds=1)) is False
    assert boundary == NOW - timedelta(seconds=120)


def test_negative_cache_expires_and_does_not_create_a_zero_value() -> None:
    clocks = MutableClocks()
    cache = BoundedLruCache(_policy(), monotonic=clocks.monotonic, wall_clock=clocks.wall)
    identity = _identity("600001")

    cache.put_negative(identity, error_code="timeout")
    negative = cache.get(identity)
    assert negative.state == "negative"
    assert negative.value is None
    assert negative.error_code == "timeout"

    clocks.advance(10.0)
    assert cache.get(identity).state == "negative"
    clocks.advance(0.001)
    assert cache.get(identity) is None


def test_pure_negative_cache_byte_estimate_contains_only_the_error_category() -> None:
    cache = BoundedLruCache(_policy())
    identity = _identity("600001")

    cache.put_negative(identity, error_code="timeout")

    status = cache.status()["daily_history"]["eastmoney"]
    expected = len(canonical_json_bytes(identity.as_dict())) + len(canonical_json_bytes({"error_code": "timeout"}))
    assert status["estimated_bytes"] == expected


def test_negative_refresh_cache_preserves_value_without_reestimating_on_recovery(monkeypatch) -> None:
    clocks = MutableClocks()
    cache = BoundedLruCache(_policy(), monotonic=clocks.monotonic, wall_clock=clocks.wall)
    identity = _identity("600001")
    source_time = NOW - timedelta(seconds=30)
    cache.put(identity, {"close": 1.0}, data_version="history-v1", source_time=source_time)

    cache.put_negative(identity, error_code="timeout")
    suppressed = cache.get(identity)

    assert suppressed.value == {"close": 1.0}
    assert suppressed.data_version == "history-v1"
    assert suppressed.source_time == source_time
    assert suppressed.error_code == "timeout"
    assert suppressed.retry_suppressed is True

    clocks.advance(10.001)
    monkeypatch.setattr(
        cache,
        "_estimate_entry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cache hit must not reserialize")),
    )
    recovered = cache.get(identity)
    assert recovered.value == {"close": 1.0}
    assert recovered.state == "refresh_due"
    assert recovered.error_code is None
    assert recovered.retry_suppressed is False


def test_cache_lru_capacity_is_deterministic_and_observable() -> None:
    clocks = MutableClocks()
    cache = BoundedLruCache(_policy(capacity=2), monotonic=clocks.monotonic, wall_clock=clocks.wall)
    first, second, third = (_identity(code) for code in ("600001", "600002", "600003"))
    cache.put(first, {"close": 1.0}, data_version="v1", source_time=NOW)
    cache.put(second, {"close": 2.0}, data_version="v2", source_time=NOW)
    assert cache.get(first).value == {"close": 1.0}
    cache.put(third, {"close": 3.0}, data_version="v3", source_time=NOW)

    assert cache.get(second) is None
    status = cache.status()["daily_history"]["eastmoney"]
    assert status["entries"] == 2
    assert status["capacity"] == 2
    assert status["eviction"] == 1
    assert 0 < status["estimated_bytes"] <= 100_000


def test_cache_evicts_business_degraded_value_before_healthy_lru_value() -> None:
    clocks = MutableClocks()
    cache = BoundedLruCache(_policy(capacity=2), monotonic=clocks.monotonic, wall_clock=clocks.wall)
    degraded, healthy, incoming = (_identity(code) for code in ("600001", "600002", "600003"))
    cache.put(
        degraded,
        {"close": 1.0},
        data_version="degraded-v1",
        source_time=NOW - timedelta(seconds=121),
    )
    cache.put(healthy, {"close": 2.0}, data_version="healthy-v1", source_time=NOW)
    assert cache.get(degraded).state == "degraded"

    cache.put(incoming, {"close": 3.0}, data_version="incoming-v1", source_time=NOW)

    assert cache.get(degraded) is None
    assert cache.get(healthy).value == {"close": 2.0}
    assert cache.get(incoming).value == {"close": 3.0}


def test_cache_rejects_out_of_order_positive_replacement() -> None:
    cache = BoundedLruCache(_policy())
    identity = _identity("600001")
    cache.put(
        identity,
        {"close": 2.0},
        data_version="a-newer-time",
        source_time=NOW,
    )

    older_time = cache.put(
        identity,
        {"close": 1.0},
        data_version="z-older-time",
        source_time=NOW - timedelta(seconds=1),
    )
    older_version = cache.put(
        identity,
        {"close": 1.5},
        data_version="0-older-version",
        source_time=NOW,
    )

    assert older_time is False
    assert older_version is False
    assert cache.get(identity).value == {"close": 2.0}
    assert cache.get(identity).data_version == "a-newer-time"


def test_cache_rejects_one_entry_larger_than_its_group_byte_limit() -> None:
    cache = BoundedLruCache(_policy(group_bytes=512))
    identity = _identity("600001")

    stored = cache.put(
        identity,
        {"payload": "x" * 1024},
        data_version="v1",
        source_time=NOW,
    )

    assert stored is False
    status = cache.status()["daily_history"]["eastmoney"]
    assert status["entries"] == 0
    assert status["load_error"] == 1


def test_cache_status_uses_insert_time_scope_counters_without_scanning_entries() -> None:
    class NoStatusScanEntries(OrderedDict[object, object]):
        def items(self) -> NoReturn:
            raise AssertionError("cache status must not scan entry payloads")

    cache = BoundedLruCache(_policy())
    identity = _identity("600001")
    cache.put(identity, {"close": 1.0}, data_version="v1", source_time=NOW)
    cache._entries = NoStatusScanEntries(cache._entries)

    status = cache.status()["daily_history"]["eastmoney"]

    assert status["entries"] == 1
    assert status["estimated_bytes"] > 0


def test_cache_insert_below_capacity_does_not_scan_existing_entries() -> None:
    class NoIterationEntries(OrderedDict[object, object]):
        def __iter__(self) -> NoReturn:
            raise AssertionError("cache insert below capacity must not scan existing entries")

    cache = BoundedLruCache(_policy(capacity=2))
    first = _identity("600001")
    second = _identity("600002")
    cache.put(first, {"close": 1.0}, data_version="v1", source_time=NOW)
    cache._entries = NoIterationEntries(cache._entries)

    assert cache.put(second, {"close": 2.0}, data_version="v2", source_time=NOW) is True


def test_cache_single_flight_capacity_check_uses_scope_counters() -> None:
    class NoIterationInflight(dict[object, object]):
        def __iter__(self) -> NoReturn:
            raise AssertionError("single-flight admission must not scan the in-flight registry")

    cache = BoundedLruCache(_policy())
    cache._inflight = NoIterationInflight()

    assert cache.coalesce(_identity("600001"), lambda: "loaded") == "loaded"


def test_cache_single_flight_coalesces_physical_load_and_cleans_registry() -> None:
    cache = BoundedLruCache(_policy())
    identity = _identity("600001")
    started = threading.Event()
    release = threading.Event()
    calls = 0
    results: list[str] = []

    def load() -> str:
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(1.0)
        return "loaded"

    threads = [threading.Thread(target=lambda: results.append(cache.coalesce(identity, load))) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert started.wait(1.0)
    time.sleep(0.02)
    release.set()
    for thread in threads:
        thread.join(1.0)

    assert calls == 1
    assert results == ["loaded", "loaded"]
    assert cache.inflight_count == 0


def test_cache_stop_clears_inflight_registry_and_releases_waiters() -> None:
    cache = BoundedLruCache(_policy())
    identity = _identity("600001")
    started = threading.Event()
    release = threading.Event()
    errors: list[str] = []

    def load() -> str:
        started.set()
        assert release.wait(1.0)
        return "late"

    owner = threading.Thread(
        target=lambda: _capture_runtime_error(errors, lambda: cache.coalesce(identity, load)),
    )
    waiter = threading.Thread(
        target=lambda: _capture_runtime_error(errors, lambda: cache.coalesce(identity, load)),
    )
    owner.start()
    assert started.wait(1.0)
    waiter.start()
    time.sleep(0.02)

    cache.stop(wait=False)
    assert cache.inflight_count == 0
    release.set()
    owner.join(1.0)
    waiter.join(1.0)

    assert sorted(errors) == ["cache stopped during load", "cache stopped during load"]


def _capture_runtime_error(errors: list[str], function) -> None:
    try:
        function()
    except RuntimeError as exc:
        errors.append(str(exc))
