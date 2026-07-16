from __future__ import annotations

import threading
from datetime import datetime

import pytest

from trader.application.runtime import RuntimeSupervisor, scheduler_interval_seconds
from trader.application.schedule import SHANGHAI


def test_supervisor_initializes_starts_ticks_and_stops() -> None:
    pipeline = FakePipeline()
    initialized: list[str] = []
    now = datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI)
    supervisor = RuntimeSupervisor(
        pipeline,
        now=lambda: now,
        initializers=(lambda: initialized.append("ready"),),
        interval_seconds=lambda _at: 60.0,
        shutdown_timeout_seconds=1.0,
    )

    assert supervisor.start() is True
    assert pipeline.ticked.wait(1.0)
    assert supervisor.start() is False
    supervisor.stop()
    supervisor.stop()

    assert initialized == ["ready"]
    assert pipeline.started == 1
    assert pipeline.stopped == 1
    assert pipeline.tick_times == [now]


def test_supervisor_does_not_restart_after_shutdown() -> None:
    supervisor = RuntimeSupervisor(
        FakePipeline(),
        now=lambda: datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI),
        interval_seconds=lambda _at: 60.0,
        shutdown_timeout_seconds=1.0,
    )
    assert supervisor.start() is True
    supervisor.stop()

    with pytest.raises(RuntimeError, match="cannot restart"):
        supervisor.start()


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
