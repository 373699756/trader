from __future__ import annotations

import threading
import time
from datetime import datetime

import pytest

from trader.infra.shutdown import ShutdownDeadline
from trader.recommendation.application.runtime.schedule import SHANGHAI
from trader.recommendation.application.runtime.supervisor import (
    RuntimeSupervisor,
    RuntimeSupervisorConfig,
    SchedulerIntervalSeconds,
)


def test_supervisor_initializes_starts_ticks_and_stops() -> None:
    pipeline = FakePipeline()
    initialized: list[str] = []
    now = datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI)
    supervisor = RuntimeSupervisor(
        pipeline,
        RuntimeSupervisorConfig(
            now=lambda: now,
            initializers=(lambda: initialized.append("ready"),),
            interval_seconds=lambda _at: 60.0,
            shutdown_timeout_seconds=1.0,
        ),
    )

    assert supervisor.start() is True
    assert pipeline.ticked.wait(1.0)
    assert [thread.name for thread in threading.enumerate()].count("trader-scheduler") == 1
    assert supervisor.start() is False
    supervisor.stop()
    supervisor.stop()

    assert initialized == ["ready"]
    assert pipeline.started == 1
    assert pipeline.stopped == 1
    assert pipeline.tick_times == [now]
    assert "trader-scheduler" not in {thread.name for thread in threading.enumerate()}


def test_supervisor_can_initialize_once_before_starting_any_threads() -> None:
    pipeline = FakePipeline()
    initialized: list[str] = []
    supervisor = RuntimeSupervisor(
        pipeline,
        RuntimeSupervisorConfig(
            now=lambda: datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI),
            initializers=(lambda: initialized.append("ready"),),
            interval_seconds=lambda _at: 60.0,
            shutdown_timeout_seconds=1.0,
        ),
    )
    supervisor.initialize()
    supervisor.initialize()
    assert initialized == ["ready"]
    assert pipeline.started == 0
    assert not pipeline.ticked.is_set()
    try:
        assert supervisor.start()
        assert pipeline.ticked.wait(1.0)
        assert initialized == ["ready"]
    finally:
        assert supervisor.stop().completed
    with pytest.raises(RuntimeError, match="cannot initialize after stop"):
        supervisor.initialize()


def test_supervisor_does_not_block_start_on_deferred_initializer() -> None:
    pipeline = FakePipeline()
    deferred_started = threading.Event()
    release_deferred = threading.Event()

    def deferred_initializer() -> None:
        deferred_started.set()
        release_deferred.wait(2.0)

    supervisor = RuntimeSupervisor(
        pipeline,
        RuntimeSupervisorConfig(
            now=lambda: datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI),
            initializers=(),
            deferred_initializers=(deferred_initializer,),
            interval_seconds=lambda _at: 60.0,
            shutdown_timeout_seconds=1.0,
        ),
    )

    started_at = time.monotonic()
    assert supervisor.start() is True
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.5
    assert deferred_started.wait(1.0)
    release_deferred.set()
    supervisor.stop()


def test_supervisor_does_not_restart_after_shutdown() -> None:
    supervisor = RuntimeSupervisor(
        FakePipeline(),
        RuntimeSupervisorConfig(
            now=lambda: datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI),
            initializers=(),
            interval_seconds=lambda _at: 60.0,
            shutdown_timeout_seconds=1.0,
        ),
    )
    assert supervisor.start() is True
    supervisor.stop()

    with pytest.raises(RuntimeError, match="cannot restart"):
        supervisor.start()


def test_supervisor_does_not_wait_without_bound_after_blocked_scheduler() -> None:
    pipeline = BlockingTickPipeline()
    errors: list[str] = []

    def record_error(error: str) -> None:
        errors.append(error)

    supervisor = RuntimeSupervisor(
        pipeline,
        RuntimeSupervisorConfig(
            now=lambda: datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI),
            initializers=(),
            interval_seconds=lambda _at: 60.0,
            shutdown_timeout_seconds=0.1,
            record_error=record_error,
        ),
    )
    assert supervisor.start() is True
    assert pipeline.tick_started.wait(timeout=1.0)

    try:
        deadline = ShutdownDeadline.start(0.05)
        report = supervisor.stop(deadline)

        assert report.completed is False
        assert report.steps[0].name == "scheduler"
        assert report.steps[0].timed_out is True
        assert errors == ["scheduler shutdown exceeded timeout"]
    finally:
        pipeline.allow_tick.set()
        supervisor.stop(ShutdownDeadline.start(1.0))
        # Repeated stop returns the cached timeout report; the fixture must reap its thread.
        assert pipeline.tick_thread is not None
        pipeline.tick_thread.join(timeout=1.0)
        assert not pipeline.tick_thread.is_alive()

    assert pipeline.stopped == 1


def test_supervisor_scheduler_start_interruption_stops_started_pipeline(monkeypatch) -> None:
    pipeline = FakePipeline()
    supervisor = RuntimeSupervisor(
        pipeline,
        RuntimeSupervisorConfig(
            now=lambda: datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI),
            initializers=(),
            interval_seconds=lambda _at: 60.0,
            shutdown_timeout_seconds=1.0,
        ),
    )

    def interrupt_scheduler_start(thread: threading.Thread) -> None:
        if thread.name == "trader-scheduler":
            raise KeyboardInterrupt
        original_start(thread)

    original_start = threading.Thread.start
    monkeypatch.setattr(threading.Thread, "start", interrupt_scheduler_start)

    with pytest.raises(KeyboardInterrupt):
        supervisor.start()

    assert pipeline.started == 1
    assert pipeline.stopped == 1
    assert "trader-scheduler" not in {thread.name for thread in threading.enumerate()}


@pytest.mark.parametrize("name", ("trader-scheduler", "trader-deferred-initializer"))
@pytest.mark.parametrize("after_start", (False, True))
def test_startup_failure_reaps_every_thread_that_was_started(monkeypatch, name, after_start) -> None:
    pipeline = FakePipeline()
    release_deferred = threading.Event()
    supervisor = RuntimeSupervisor(
        pipeline,
        RuntimeSupervisorConfig(
            now=lambda: datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI),
            initializers=(),
            deferred_initializers=(lambda: release_deferred.wait(2.0),),
            interval_seconds=lambda _at: 60.0,
            shutdown_timeout_seconds=0.2,
        ),
    )
    threads: list[threading.Thread] = []
    original_start = threading.Thread.start

    def failing_start(thread: threading.Thread) -> None:
        threads.append(thread)
        if thread.name == name:
            if after_start:
                original_start(thread)
                if name == "trader-deferred-initializer":
                    release_deferred.set()
            raise RuntimeError("controlled_thread_start_failure")
        original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", failing_start)
    try:
        with pytest.raises(RuntimeError, match="controlled_thread_start_failure"):
            supervisor.start()
        assert pipeline.stopped == 1
        assert not any(
            thread.is_alive()
            for thread in threads
            if thread.name in {"trader-scheduler", "trader-deferred-initializer"}
        )
    finally:
        release_deferred.set()
        supervisor._stop_event.set()
        supervisor._deferred_stop_event.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(1.0)
        supervisor.stop()


def test_stop_reports_a_blocked_deferred_initializer_and_skips_later_initializers() -> None:
    pipeline = FakePipeline()
    entered = threading.Event()
    release = threading.Event()
    errors: list[str] = []
    later: list[str] = []
    threads: list[threading.Thread] = []

    def initialize() -> None:
        threads.append(threading.current_thread())
        entered.set()
        release.wait(2.0)

    supervisor = RuntimeSupervisor(
        pipeline,
        RuntimeSupervisorConfig(
            now=lambda: datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI),
            initializers=(),
            deferred_initializers=(initialize, lambda: later.append("unexpected")),
            interval_seconds=lambda _at: 60.0,
            shutdown_timeout_seconds=0.05,
            record_error=errors.append,
        ),
    )
    try:
        assert supervisor.start()
        assert entered.wait(1.0)
        report = supervisor.stop()
        assert not report.completed
        step = next(step for step in report.steps if step.name == "deferred_initializers")
        assert not step.completed and step.timed_out
        assert "deferred initializer shutdown exceeded timeout" in errors
    finally:
        release.set()
        for thread in threads:
            thread.join(1.0)
            assert not thread.is_alive()
        supervisor.stop()
    assert later == []


def test_shutdown_deadline_uses_the_injected_monotonic_clock() -> None:
    deadlines = []
    monotonic = lambda: 1234.0

    class Pipeline(FakePipeline):
        def stop(self, *, deadline: ShutdownDeadline) -> None:
            deadlines.append(deadline)
            super().stop(deadline=deadline)

    supervisor = RuntimeSupervisor(
        Pipeline(),
        RuntimeSupervisorConfig(
            now=lambda: datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI),
            initializers=(),
            interval_seconds=lambda _at: 60.0,
            shutdown_timeout_seconds=1.0,
            monotonic=monotonic,
        ),
    )
    try:
        assert supervisor.start()
    finally:
        assert supervisor.stop().completed
    assert len(deadlines) == 1
    assert deadlines[0].started_at_monotonic == 1234.0
    assert deadlines[0].monotonic is monotonic


def test_scheduler_cadence_stays_ten_seconds_until_freeze() -> None:
    morning = datetime(2026, 7, 16, 9, 20, tzinfo=SHANGHAI)
    main = datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI)
    final_quote = datetime(2026, 7, 16, 14, 49, 55, tzinfo=SHANGHAI)

    assert SchedulerIntervalSeconds(morning) == 10.0
    assert SchedulerIntervalSeconds(main) == 10.0
    assert SchedulerIntervalSeconds(final_quote) == 10.0


class FakePipeline:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.tick_times: list[datetime] = []
        self.ticked = threading.Event()

    def start(self) -> bool:
        self.started += 1
        return True

    def stop(self, *, deadline: ShutdownDeadline) -> None:
        assert deadline.timeout_seconds > 0.0
        self.stopped += 1

    def submit_due(self, at: datetime | None = None) -> float:
        if at is not None:
            self.tick_times.append(at)
        self.ticked.set()
        return 60.0


class BlockingTickPipeline(FakePipeline):
    def __init__(self) -> None:
        super().__init__()
        self.tick_started = threading.Event()
        self.allow_tick = threading.Event()
        self.tick_thread: threading.Thread | None = None

    def submit_due(self, at: datetime | None = None) -> float:
        self.tick_thread = threading.current_thread()
        self.tick_started.set()
        self.allow_tick.wait()
        return super().submit_due(at)
