from __future__ import annotations

import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from trader.infra.shutdown import ShutdownDeadline, ShutdownStep
from trader.infra.workers import BoundedExecutor
from trader.recommendation.domain.market.refresh import ResearchRefreshResult
from trader.training.application.research_coordination import ResearchCoordinator, ResearchCoordinatorOptions

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 29, 19, 30, tzinfo=SHANGHAI)


class MutableMonotonic:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class RecordingResearch:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.completed = threading.Event()

    def refresh_stock_risk(self, codes, observed_at, *, deadline=None):
        batch = tuple(codes)
        self.calls.append(batch)
        if sum(len(item) for item in self.calls) >= 6:
            self.completed.set()
        return ResearchRefreshResult(
            requested_codes=batch,
            completed_codes=batch,
            changed_codes=batch,
            covered_codes=batch,
            data_version="research:test",
            started_at=observed_at,
            completed_at=observed_at + timedelta(seconds=1),
        )


def test_research_lifecycle_accepts_a_narrow_executor_and_never_runs_rejected_work_inline() -> None:
    class RejectingExecutor:
        def __init__(self):
            self.running = False
            self.stop_deadlines = []

        def start(self):
            self.running = True
            return True

        def submit(self, function, /, *args, **kwargs):
            return None

        def stop(self, *, wait=True, cancel_futures=False, deadline=None):
            assert cancel_futures
            self.running = False
            self.stop_deadlines.append(deadline)
            return ShutdownStep("fixture-executor", True, False)

    executor = RejectingExecutor()
    research = RecordingResearch()
    results = []
    coordinator = ResearchCoordinator(research, executor, now=lambda: NOW, on_result=results.append)
    assert coordinator.start()
    assert not coordinator.start()
    assert executor.running
    assert not coordinator.offer(("600001",), NOW)
    assert coordinator.status().last_error == "research_coordinator_queue_rejected"
    assert research.calls == results == []
    deadline = ShutdownDeadline.start(1.0)
    assert coordinator.stop(wait=True, deadline=deadline).completed
    assert coordinator.stop(wait=True, deadline=deadline).completed
    assert not executor.running
    assert executor.stop_deadlines == [deadline, deadline]
    assert not coordinator.offer(("600001",), NOW)


def test_research_coordinator_runs_after_close_in_bounded_priority_batches() -> None:
    research = RecordingResearch()
    results: list[ResearchRefreshResult] = []
    coordinator = ResearchCoordinator(
        research,
        BoundedExecutor(worker_count=1, queue_capacity=1, thread_name_prefix="test-company-research"),
        now=lambda: NOW,
        on_result=results.append,
        options=ResearchCoordinatorOptions(
            batch_size=4,
            batch_budget_seconds=40,
        ),
    )
    assert coordinator.start() is True
    try:
        coordinator.offer(("600006", "600005", "600004", "600003", "600002", "600001"), NOW)
        assert research.completed.wait(2.0)
    finally:
        coordinator.stop(wait=True)

    assert research.calls == [
        ("600006", "600005", "600004", "600003"),
        ("600002", "600001"),
    ]
    assert [item.completed_codes for item in results] == [
        ("600006", "600005", "600004", "600003"),
        ("600002", "600001"),
    ]
    assert coordinator.status().state == "stopped"


def test_research_coordinator_prioritizes_new_codes_without_cancelling_running_batch() -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    calls: list[tuple[str, ...]] = []

    class BlockingResearch:
        def refresh_stock_risk(self, codes, observed_at, *, deadline=None):
            batch = tuple(codes)
            calls.append(batch)
            if len(calls) == 1:
                first_started.set()
                release_first.wait(2.0)
            return ResearchRefreshResult(
                requested_codes=batch,
                completed_codes=batch,
                changed_codes=batch,
                covered_codes=batch,
                data_version=f"research:{len(calls)}",
                started_at=observed_at,
                completed_at=observed_at,
            )

    coordinator = ResearchCoordinator(
        BlockingResearch(),
        BoundedExecutor(worker_count=1, queue_capacity=1, thread_name_prefix="test-company-research"),
        now=lambda: NOW,
        on_result=lambda _result: None,
        options=ResearchCoordinatorOptions(
            batch_size=2,
            batch_budget_seconds=40,
        ),
    )
    coordinator.start()
    try:
        coordinator.offer(("600001", "600002", "600003"), NOW)
        assert first_started.wait(1.0)
        coordinator.offer(("600009", "600003"), NOW)
        release_first.set()
        assert coordinator.wait_until_idle(2.0)
    finally:
        release_first.set()
        coordinator.stop(wait=True)

    assert calls[0] == ("600001", "600002")
    assert calls[1] == ("600009", "600003")


def test_research_coordinator_cools_successful_codes_between_explicit_offers() -> None:
    monotonic = MutableMonotonic()
    research = RecordingResearch()
    coordinator = ResearchCoordinator(
        research,
        BoundedExecutor(worker_count=1, queue_capacity=1, thread_name_prefix="test-company-research"),
        now=lambda: NOW,
        on_result=lambda _result: None,
        options=ResearchCoordinatorOptions(
            batch_size=4,
            success_cooldown_seconds=60,
            retry_delays_seconds=(60, 120),
            monotonic=monotonic,
        ),
    )
    coordinator.start()
    try:
        assert coordinator.offer(("600001",), NOW) is True
        assert coordinator.wait_until_idle(2.0)
        for _ in range(300):
            assert coordinator.offer(("600001",), NOW) is False
        assert research.calls == [("600001",)]
        status = coordinator.status()
        assert status.cooldown_codes == 1
        assert status.retry_wait_codes == 0
        assert status.gated_offer_codes == 300

        monotonic.advance(60)
        assert coordinator.offer(("600001",), NOW + timedelta(minutes=1)) is True
        assert coordinator.wait_until_idle(2.0)
    finally:
        coordinator.stop(wait=True)

    assert research.calls == [("600001",), ("600001",)]


def test_research_coordinator_full_failure_short_circuits_pending_batches() -> None:
    monotonic = MutableMonotonic()
    calls: list[tuple[str, ...]] = []

    class FailedResearch:
        def refresh_stock_risk(self, codes, observed_at, *, deadline=None):
            batch = tuple(codes)
            calls.append(batch)
            return ResearchRefreshResult(
                requested_codes=batch,
                failed_codes=batch,
                data_version=f"research:failed:{len(calls)}",
                started_at=observed_at,
                completed_at=observed_at,
            )

    coordinator = ResearchCoordinator(
        FailedResearch(),
        BoundedExecutor(worker_count=1, queue_capacity=1, thread_name_prefix="test-company-research"),
        now=lambda: NOW,
        on_result=lambda _result: None,
        options=ResearchCoordinatorOptions(
            batch_size=2,
            success_cooldown_seconds=60,
            retry_delays_seconds=(60, 120, 240),
            monotonic=monotonic,
        ),
    )
    coordinator.start()
    try:
        all_codes = ("600001", "600002", "600003", "600004", "600005", "600006")
        assert coordinator.offer(all_codes, NOW) is True
        assert coordinator.wait_until_idle(2.0)
        assert calls == [("600001", "600002")]
        status = coordinator.status()
        assert status.short_circuited_batches == 1
        assert status.short_circuited_codes == 4
        assert status.retry_wait_codes == 6
        assert status.next_retry_seconds == 60.0

        for _ in range(30):
            assert coordinator.offer(all_codes, NOW) is False
        assert calls == [("600001", "600002")]

        monotonic.advance(60)
        assert coordinator.offer(all_codes, NOW + timedelta(minutes=1)) is True
        assert coordinator.wait_until_idle(2.0)
        assert calls == [("600001", "600002"), ("600001", "600002")]
        status = coordinator.status()
        assert status.short_circuited_batches == 2
        assert status.retry_wait_codes == 6
        assert status.next_retry_seconds == 120.0
    finally:
        coordinator.stop(wait=True)


def test_research_coordinator_retries_partial_codes_without_blocking_covered_codes() -> None:
    monotonic = MutableMonotonic()

    class PartialResearch:
        def refresh_stock_risk(self, codes, observed_at, *, deadline=None):
            batch = tuple(codes)
            return ResearchRefreshResult(
                requested_codes=batch,
                completed_codes=batch,
                partial_codes=(batch[0],),
                covered_codes=(batch[1],),
                data_version="research:partial",
                started_at=observed_at,
                completed_at=observed_at,
            )

    coordinator = ResearchCoordinator(
        PartialResearch(),
        BoundedExecutor(worker_count=1, queue_capacity=1, thread_name_prefix="test-company-research"),
        now=lambda: NOW,
        on_result=lambda _result: None,
        options=ResearchCoordinatorOptions(
            batch_size=2,
            success_cooldown_seconds=30,
            retry_delays_seconds=(60, 120),
            monotonic=monotonic,
        ),
    )
    coordinator.start()
    try:
        assert coordinator.offer(("600001", "600002"), NOW) is True
        assert coordinator.wait_until_idle(2.0)
        status = coordinator.status()
        assert status.cooldown_codes == 1
        assert status.retry_wait_codes == 1
        assert status.next_retry_seconds == 60.0
    finally:
        coordinator.stop(wait=True)
