from __future__ import annotations

from concurrent.futures import Future
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from trader.application.ports.market import MarketDataDeadlineExceededError
from trader.infra.market_data.service_history_warmup import HistoryWarmup

NOW = datetime(2026, 7, 24, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class _Clock:
    value = 0.0

    def __call__(self) -> float:
        return self.value


class _History:
    def entries(self):
        return {}

    def available_seed_codes(self, _codes):
        return ()

    def load(self, _codes, *, deadline=None):
        del deadline
        return {}

    def update_coverage(self, _codes):
        return None


class _References:
    def health(self):
        return {}


class _Lanes:
    def __init__(self) -> None:
        self.submissions: list[tuple[tuple[str, ...], Future[object]]] = []
        self.deadlines: list[datetime | None] = []

    def submit(self, _source, _identity, _observed_at, _function, codes, **kwargs):
        future: Future[object] = Future()
        self.submissions.append((tuple(codes), future))
        self.deadlines.append(kwargs.get("deadline"))
        return future

    def is_stopped(self, _source):
        return False


def test_failed_history_codes_cool_down_while_unattempted_codes_continue() -> None:
    clock = _Clock()
    lanes = _Lanes()
    runner = SimpleNamespace(source_lanes=lanes, wall_clock=lambda: NOW)
    warmup = HistoryWarmup(
        _History(),
        _References(),
        runner,
        batch_size=2,
        batch_timeout_seconds=5.0,
        monotonic=clock,
    )
    codes = ("600001", "600002", "300001", "300002")

    warmup.schedule_history_warmup(codes, NOW - timedelta(minutes=1))
    first_codes, first = lanes.submissions[0]
    assert lanes.deadlines[0] == NOW + timedelta(seconds=5)
    first.set_result({})

    assert first_codes == ("600001", "600002")
    assert lanes.submissions[1][0] == ("300001", "300002")
    lanes.submissions[1][1].set_result({})
    assert len(lanes.submissions) == 2
    status = warmup.status()
    assert status.planned_count == 4
    assert status.failure_count == 4
    assert status.retry_deferred_count == 4
    assert status.unique_failure_count == 4
    assert status.next_retry_seconds == 60.0

    clock.value = 59.0
    warmup.schedule_history_warmup(codes, NOW)
    assert len(lanes.submissions) == 2
    clock.value = 60.0
    warmup.schedule_history_warmup(codes, NOW)
    assert lanes.submissions[2][0] == ("600001", "600002")
    lanes.submissions[2][1].set_result({})
    assert lanes.submissions[3][0] == ("300001", "300002")
    lanes.submissions[3][1].set_result({})
    assert warmup.status().next_retry_seconds == 120.0
    clock.value = 179.0
    warmup.schedule_history_warmup(codes, NOW)
    assert len(lanes.submissions) == 4
    clock.value = 180.0
    warmup.schedule_history_warmup(codes, NOW)
    assert lanes.submissions[4][0] == ("600001", "600002")


def test_timed_out_history_batch_releases_inflight_and_reports_age() -> None:
    clock = _Clock()
    lanes = _Lanes()
    runner = SimpleNamespace(source_lanes=lanes, wall_clock=lambda: NOW)
    warmup = HistoryWarmup(
        _History(),
        _References(),
        runner,
        batch_size=2,
        batch_timeout_seconds=5.0,
        monotonic=clock,
    )
    codes = ("600001", "600002", "300001", "300002")

    warmup.schedule_history_warmup(codes, NOW)
    clock.value = 4.0

    assert warmup.status().inflight_age_seconds == 4.0
    first = lanes.submissions[0][1]
    first.set_exception(MarketDataDeadlineExceededError("history preload exceeded its batch deadline"))

    status = warmup.status()
    assert status.timeout_count == 1
    assert status.batch_timeout_seconds == 5.0
    assert status.failure_count == 2
    assert status.inflight_count == 2
    assert status.inflight_age_seconds == 0.0
    assert lanes.submissions[1][0] == ("300001", "300002")
