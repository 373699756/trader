from __future__ import annotations

from concurrent.futures import Future
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from trader.application.ports.market import MarketDataDeadlineExceededError
from trader.application.runtime.source_lanes import SourceLaneRegistryStatus, SourceLaneStatus
from trader.infra.market_data.history.service_history_warmup import HistoryWarmup, build_history_warmup_policy

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
    def health(self) -> None:
        return None


def _all_eligible(codes, _observed_at):
    return tuple(codes)


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

    def status(self):
        return SourceLaneRegistryStatus(
            {
                "history": SourceLaneStatus(
                    source="history",
                    running=False,
                    pending=False,
                    completed_count=0,
                    coalesced_count=0,
                    superseded_count=0,
                    rejected_count=0,
                    stopped=False,
                )
            }
        )


def test_production_warmup_policy_never_queues_more_than_one_worker_wave() -> None:
    policy = build_history_warmup_policy(
        worker_count=5,
        source_timeout_seconds=12.0,
        maximum_batch_size=30,
        maximum_batch_timeout_seconds=20.0,
    )

    assert policy.batch_size == 5
    assert policy.batch_timeout_seconds == 20.0
    assert policy.source_attempt_timeout_seconds == 4.5


def test_warmup_policy_keeps_shorter_configured_source_timeout() -> None:
    policy = build_history_warmup_policy(
        worker_count=5,
        source_timeout_seconds=2.0,
        maximum_batch_size=30,
        maximum_batch_timeout_seconds=20.0,
    )

    assert policy.source_attempt_timeout_seconds == 2.0


def test_failed_history_codes_cool_down_while_unattempted_codes_continue() -> None:
    clock = _Clock()
    lanes = _Lanes()
    runner = SimpleNamespace(source_lanes=lanes, wall_clock=lambda: NOW)
    warmup = HistoryWarmup(
        _History(),
        _References(),
        runner,
        eligibility_filter=_all_eligible,
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
        eligibility_filter=_all_eligible,
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


def test_history_warmup_keeps_slot_order_stable_when_market_ranking_reorders() -> None:
    clock = _Clock()
    lanes = _Lanes()
    runner = SimpleNamespace(source_lanes=lanes, wall_clock=lambda: NOW)
    warmup = HistoryWarmup(
        _History(),
        _References(),
        runner,
        eligibility_filter=_all_eligible,
        batch_size=2,
        batch_timeout_seconds=5.0,
        monotonic=clock,
    )
    original = ("600001", "300001", "600002", "300002")

    warmup.schedule_history_warmup(original, NOW)
    warmup.schedule_history_warmup(tuple(reversed(original)), NOW + timedelta(seconds=1))
    lanes.submissions[0][1].set_result({})

    assert lanes.submissions[0][0] == original[:2]
    assert lanes.submissions[1][0] == original[2:]


def test_permanently_excluded_codes_never_enter_history_lane() -> None:
    clock = _Clock()
    lanes = _Lanes()
    runner = SimpleNamespace(source_lanes=lanes, wall_clock=lambda: NOW)
    warmup = HistoryWarmup(
        _History(),
        _References(),
        runner,
        eligibility_filter=lambda codes, _observed_at: tuple(code for code in codes if code != "600001"),
        batch_size=2,
        batch_timeout_seconds=5.0,
        monotonic=clock,
    )

    warmup.schedule_history_warmup(("600001", "600002"), NOW)

    assert lanes.submissions[0][0] == ("600002",)
    assert warmup.status().excluded_count == 1
