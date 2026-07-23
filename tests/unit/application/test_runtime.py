from __future__ import annotations

import threading
from datetime import datetime

import pytest

from trader.application.runtime import RuntimeSupervisor, RuntimeSupervisorConfig, scheduler_interval_seconds
from trader.application.schedule import SHANGHAI


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


def test_supervisor_waits_for_blocked_scheduler_before_returning() -> None:
    pipeline = BlockingTickPipeline()
    errors: list[str] = []
    shutdown_timed_out = threading.Event()

    def record_error(error: str) -> None:
        errors.append(error)
        shutdown_timed_out.set()

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
    stopper = threading.Thread(target=supervisor.stop, name="test-supervisor-stop")

    try:
        stopper.start()
        assert shutdown_timed_out.wait(timeout=1.0)
        assert stopper.is_alive()
        assert errors == ["scheduler shutdown exceeded timeout"]
    finally:
        pipeline.allow_tick.set()
        stopper.join(timeout=1.0)
        supervisor.stop()

    assert not stopper.is_alive()
    assert pipeline.stopped == 1
    assert "trader-scheduler" not in {thread.name for thread in threading.enumerate()}


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
        assert thread.name == "trader-scheduler"
        raise KeyboardInterrupt

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
