from __future__ import annotations

import threading
import time
from datetime import datetime

import pytest

from trader.application.runtime import RuntimeSupervisor, RuntimeSupervisorConfig, scheduler_interval_seconds
from trader.application.schedule import SHANGHAI
from trader.application.shutdown import ShutdownDeadline


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


def test_scheduler_cadence_tightens_at_final_quote() -> None:
    morning = datetime(2026, 7, 16, 9, 20, tzinfo=SHANGHAI)
    main = datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI)
    final_quote = datetime(2026, 7, 16, 14, 49, 55, tzinfo=SHANGHAI)

    assert scheduler_interval_seconds(morning) == 60.0
    assert scheduler_interval_seconds(main) == 10.0
    assert scheduler_interval_seconds(final_quote) == 2.0


class FakePipeline:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.tick_times: list[datetime] = []
        self.ticked = threading.Event()

    def start(self) -> bool:
        self.started += 1
        return True

    def stop(self, timeout_seconds: float = 15.0) -> None:
        self.stopped += 1

    def submit_tick(self, at: datetime | None = None) -> bool:
        if at is not None:
            self.tick_times.append(at)
        self.ticked.set()
        return True


class BlockingTickPipeline(FakePipeline):
    def __init__(self) -> None:
        super().__init__()
        self.tick_started = threading.Event()
        self.allow_tick = threading.Event()

    def submit_tick(self, at: datetime | None = None) -> bool:
        self.tick_started.set()
        self.allow_tick.wait()
        return super().submit_tick(at)
