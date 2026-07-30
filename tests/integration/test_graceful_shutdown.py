"""Integration coverage for bounded graceful process shutdown."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from trader.application.events import (
    BoundedEventQueue,
    EventPriority,
    EventSpec,
    EventStatus,
    InMemoryEventLedger,
    new_event,
)
from trader.application.ports.market import ResearchRefreshResult
from trader.application.research_coordination import ResearchCoordinator, ResearchCoordinatorOptions
from trader.application.shutdown import ShutdownDeadline, ShutdownSignalController, signal_exit_code
from trader.application.source_lanes import SourceLaneRegistry
from trader.application.workers import BoundedExecutor
from trader.domain.recommendation.models import Strategy


def test_shutdown_deadline_is_absolute_and_never_resets_between_steps() -> None:
    monotonic_values = iter((103.0, 109.5, 111.0, 112.0))
    deadline = ShutdownDeadline(
        started_at_monotonic=100.0,
        timeout_seconds=10.0,
        monotonic=lambda: next(monotonic_values),
    )

    assert deadline.remaining_seconds() == 7.0
    assert deadline.remaining_seconds() == 0.5
    assert deadline.expired is True
    assert deadline.remaining_seconds() == 0.0


def test_second_shutdown_signal_forces_immediate_exit() -> None:
    requested: list[ShutdownDeadline] = []
    forced: list[int] = []
    controller = ShutdownSignalController(
        timeout_seconds=30.0,
        on_first_signal=requested.append,
        force_exit=forced.append,
    )

    controller.handle(2)
    controller.handle(2)

    assert len(requested) == 1
    assert requested[0].timeout_seconds == 30.0
    assert forced == [130]
    controller.mark_completed()


def test_signal_exit_codes_match_shell_conventions() -> None:
    assert signal_exit_code(2) == 130
    assert signal_exit_code(15) == 143


def test_queue_close_cancels_normal_events_and_drains_freeze_before_risk() -> None:
    now = datetime.fromisoformat("2026-07-16T14:50:00+08:00")
    ledger = InMemoryEventLedger()
    event_queue = BoundedEventQueue(maximum_size=5, reserved_priority_size=2)
    normal = _event(now, "quote", EventPriority.MARKET_QUOTES, "600001")
    risk = _event(now, "risk", EventPriority.RISK, "600002")
    freeze = _event(now, "freeze", EventPriority.FREEZE, "market")
    for event in (normal, risk, freeze):
        assert ledger.reserve_event(event.audit_record(status=EventStatus.PENDING))
        assert event_queue.put(event)

    result = event_queue.close(ledger=ledger)

    assert result.cancelled_event_ids == (normal.event_id,)
    assert result.preserved_event_ids == (freeze.event_id, risk.event_id)
    assert event_queue.get() == freeze
    assert event_queue.get() == risk
    assert event_queue.get() is None
    assert ledger.event(normal.event_id).status is EventStatus.FAILED


def test_source_lanes_share_one_deadline_instead_of_waiting_per_lane() -> None:
    pool = BoundedExecutor(worker_count=1, queue_capacity=1, thread_name_prefix="shutdown-source")
    lanes = SourceLaneRegistry(pool)
    entered = threading.Event()
    release = threading.Event()
    pool.start()
    future = lanes.submit(
        "eastmoney",
        "blocked",
        datetime.fromisoformat("2026-07-16T14:50:00+08:00"),
        lambda: (entered.set(), release.wait()),
    )
    assert entered.wait(timeout=1.0)

    started = time.monotonic()
    try:
        steps = lanes.stop(deadline=ShutdownDeadline.start(0.05))
        elapsed = time.monotonic() - started

        assert elapsed < 0.2
        assert steps[0].timed_out is True
        assert all(step.timed_out or step.completed for step in steps)
    finally:
        release.set()
        future.result(timeout=1.0)
        pool.stop(deadline=ShutdownDeadline.start(1.0), cancel_futures=True)


def test_research_coordinator_returns_at_shared_deadline_when_endpoint_blocks() -> None:
    entered = threading.Event()
    release = threading.Event()
    now = datetime.fromisoformat("2026-07-16T14:50:00+08:00")

    class BlockingResearch:
        def refresh_stock_risk(self, codes, observed_at, *, deadline):
            entered.set()
            release.wait()
            return ResearchRefreshResult(
                requested_codes=tuple(codes),
                data_version="research-v1",
                started_at=observed_at,
                completed_at=observed_at,
            )

    coordinator = ResearchCoordinator(
        BlockingResearch(),
        now=lambda: now,
        on_result=lambda _result: None,
        options=ResearchCoordinatorOptions(batch_budget_seconds=1.0),
    )
    coordinator.start()
    assert coordinator.offer(("600001",), now)
    assert entered.wait(timeout=1.0)

    try:
        step = coordinator.stop(
            wait=True,
            deadline=ShutdownDeadline.start(0.05),
        )
        assert step.completed is False
        assert step.timed_out is True
    finally:
        release.set()
        coordinator.stop(wait=True, deadline=ShutdownDeadline.start(1.0))


@pytest.mark.skipif(os.name == "nt", reason="POSIX subprocess signals")
@pytest.mark.parametrize(
    ("shutdown_signal", "expected_code"),
    ((signal.SIGINT, 130), (signal.SIGTERM, 143)),
)
def test_entrypoint_signal_exit_code_and_drain(
    tmp_path: Path,
    shutdown_signal: signal.Signals,
    expected_code: int,
) -> None:
    marker = tmp_path / "stopped"
    process = _signal_process(marker, stop_delay=0.0)
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "READY"
        process.send_signal(shutdown_signal)
        assert process.wait(timeout=3.0) == expected_code
        assert marker.read_text(encoding="utf-8") == "stopped"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1.0)


@pytest.mark.skipif(os.name == "nt", reason="POSIX subprocess signals")
def test_second_signal_forces_exit_while_cleanup_is_blocked(tmp_path: Path) -> None:
    process = _signal_process(tmp_path / "not-written", stop_delay=10.0)
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "READY"
        process.send_signal(signal.SIGINT)
        assert process.stdout.readline().strip() == "STOPPING"
        started = time.monotonic()
        process.send_signal(signal.SIGINT)

        assert process.wait(timeout=2.0) == 130
        assert time.monotonic() - started < 1.0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1.0)


@pytest.mark.skipif(os.name == "nt", reason="POSIX subprocess signals")
def test_shutdown_deadline_hard_exits_with_code_two(tmp_path: Path) -> None:
    marker = tmp_path / "not-written"
    process = _signal_process(marker, stop_delay=10.0)
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "READY"
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)

        assert process.wait(timeout=3.0) == 2
        assert 0.8 <= time.monotonic() - started < 2.0
        assert not marker.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1.0)


@pytest.mark.skipif(os.name == "nt", reason="POSIX subprocess signals")
def test_signal_during_startup_still_uses_graceful_deadline(tmp_path: Path) -> None:
    marker = tmp_path / "startup-stopped"
    process = _startup_signal_process(marker, start_delay=0.2)
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "READY"
        process.send_signal(signal.SIGTERM)

        assert process.wait(timeout=3.0) == 143
        assert marker.read_text(encoding="utf-8") == "stopped"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1.0)


def _event(
    at: datetime,
    event_type: str,
    priority: EventPriority,
    subject: str,
):
    payload = {"freeze_strategies": [Strategy.TOMORROW.value]} if priority is EventPriority.FREEZE else {}
    return new_event(
        EventSpec(
            event_type=event_type,
            subject_key=subject,
            trade_date="2026-07-16",
            phase="afternoon",
            strategy=None,
            priority=priority,
            data_version=f"{event_type}-v1",
            config_version="runtime-v2",
            created_at=at,
            payload=payload,
        )
    )


def _signal_process(marker: Path, *, stop_delay: float) -> subprocess.Popen[str]:
    script = """
import sys
import threading
import time
from pathlib import Path
from trader.entrypoints.server import _serve_until_signal

marker = Path(sys.argv[1])
delay = float(sys.argv[2])

class Server:
    def __init__(self):
        self.stopped = threading.Event()
    def serve_forever(self):
        print("READY", flush=True)
        self.stopped.wait()
    def shutdown(self):
        self.stopped.set()
    def server_close(self):
        pass

class System:
    def stop(self):
        print("STOPPING", flush=True)
        time.sleep(delay)
        marker.write_text("stopped", encoding="utf-8")

raise SystemExit(_serve_until_signal(Server(), System(), timeout_seconds=1.0))
"""
    return subprocess.Popen(
        (sys.executable, "-c", script, str(marker), str(stop_delay)),
        cwd=Path(__file__).parents[2],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _startup_signal_process(marker: Path, *, start_delay: float) -> subprocess.Popen[str]:
    script = """
import sys
import time
from pathlib import Path
from trader.entrypoints.server import _run_system

marker = Path(sys.argv[1])
delay = float(sys.argv[2])

class System:
    def start(self):
        print("READY", flush=True)
        time.sleep(delay)
        return True
    def stop(self):
        marker.write_text("stopped", encoding="utf-8")

raise SystemExit(_run_system(System(), timeout_seconds=1.0))
"""
    return subprocess.Popen(
        (sys.executable, "-c", script, str(marker), str(start_delay)),
        cwd=Path(__file__).parents[2],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
