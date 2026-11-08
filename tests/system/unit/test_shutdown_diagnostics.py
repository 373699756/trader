from __future__ import annotations

import threading
import logging
import os
import subprocess
import sys
from contextlib import ExitStack
from types import SimpleNamespace

import pytest

from tests.helpers.recommendation.runtime.scheduler import _round_cleanup_runtime
from trader.entrypoints import server as server_entrypoint
from trader.infra.shutdown import (
    CollectShutdownFailures,
    ObserveShutdownStep,
    PendingShutdownSteps,
    ReadShutdownTimeline,
    RecordShutdownEvent,
    ShutdownDeadline,
    ShutdownReport,
    ShutdownSignalController,
    ShutdownStep,
)
from trader.recommendation.application.runtime.resource_orchestration import ApplicationResources, StopApplicationResources
from trader.recommendation.infra.runtime.profiled_application import ProfiledApplicationSystem


class _UnusedResource:
    def start(self):
        return True

    def stop(self, *, wait=True, cancel_futures=False, deadline=None):
        return ShutdownStep("unused", True, False)


class _UnusedSources:
    def stop(self, *, wait=True, deadline=None):
        return ()


class _UnusedBudget:
    def close(self):
        return None


@pytest.mark.parametrize("interruption", (KeyboardInterrupt, SystemExit))
def test_shutdown_failure_collection_preserves_process_control_exceptions(interruption):
    deadline = ShutdownDeadline.start(1)
    steps: list[ShutdownStep] = []
    with pytest.raises(interruption), CollectShutdownFailures(deadline, steps, "controlled:stop"):
        raise interruption()
    assert steps == []
    assert PendingShutdownSteps(deadline) == ()


@pytest.mark.parametrize("blocked_owner", ("control", "score_wave"))
def test_outer_shutdown_timeout_reports_the_profile_and_blocked_runtime_owner(monkeypatch, caplog, blocked_owner):
    scheduler = _round_cleanup_runtime()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def block_step(**_options):
        entered.set()
        assert release.wait(3), "controlled shutdown blocker was not released"
        return ShutdownStep("controlled-control", True, False)

    monkeypatch.setattr(scheduler._control if blocked_owner == "control" else scheduler._score_lane, "stop", block_step)

    class Supervisor:
        def stop(self, deadline):
            return scheduler.stop(deadline)

    resources = ApplicationResources(
        Supervisor(),
        _UnusedSources(),
        _UnusedResource(),
        _UnusedResource(),
        _UnusedResource(),
        _UnusedResource(),
        (),
        _UnusedResource(),
        _UnusedBudget(),
        False,
    )

    class Profile:
        profile_id = "v5"

        def stop(self, *, deadline):
            return StopApplicationResources(resources, deadline=deadline)

    profiled = ProfiledApplicationSystem(SimpleNamespace(), None, (Profile(),))

    class System:
        def stop(self, *, deadline):
            try:
                return profiled.stop(deadline=deadline)
            finally:
                finished.set()

    system = System()
    deadline = ShutdownDeadline.start(0.25)
    try:
        report = server_entrypoint._stop_system(system, deadline)
        assert entered.is_set()
        assert not report.completed
        steps = {step.name: step for step in report.steps}
        blocked_name = f"runtime:{blocked_owner}"
        assert {"profile:v5", "resources:supervisor", blocked_name} <= steps.keys()
        assert all(not steps[name].completed and steps[name].timed_out for name in (
            "profile:v5", "resources:supervisor", blocked_name,
        ))
        server_entrypoint._log_shutdown_report(report)
        assert all(name in caplog.text for name in ("profile:v5", "resources:supervisor", blocked_name))
    finally:
        release.set()
        assert finished.wait(3), "controlled shutdown thread did not finish"
    assert PendingShutdownSteps(deadline) == ()


def test_completed_shutdown_keeps_the_original_report_without_diagnostic_steps():
    deadline = ShutdownDeadline.start(1)
    expected = ShutdownReport.from_steps(deadline, (ShutdownStep("original", True, False),))

    class System:
        def stop(self, *, deadline):
            return expected

    assert server_entrypoint._stop_system(System(), deadline) is expected


def test_shutdown_requires_the_shared_deadline_and_a_real_resource_report():
    deadline = ShutdownDeadline.start(1)
    received = []

    class InvalidSystem:
        def stop(self, *, deadline):
            received.append(deadline)
            return None

    report = server_entrypoint._stop_system(InvalidSystem(), deadline)
    assert received == [deadline]
    assert not report.completed
    assert report.steps[0].detail == "system shutdown failed:TypeError"


def test_shutdown_rejects_a_consumer_without_the_deadline_contract():
    called = []

    class InvalidSystem:
        def stop(self):
            called.append(True)

    report = server_entrypoint._stop_system(InvalidSystem(), ShutdownDeadline.start(1))
    assert called == []
    assert not report.completed
    assert report.steps[0].detail == "system shutdown failed:TypeError"


def test_pending_shutdown_steps_share_the_deadline_and_release_after_errors():
    deadline = ShutdownDeadline.start(1)
    entered, release = threading.Event(), threading.Event()

    def block():
        with ObserveShutdownStep(deadline, "runtime:score_wave"):
            entered.set()
            assert release.wait(3)

    worker = threading.Thread(target=block, name="test-shutdown-owner")
    worker.start()
    try:
        assert entered.wait(1)
        with pytest.raises(RuntimeError, match="controlled"):
            with ObserveShutdownStep(deadline, "profile:v5"):
                pending = PendingShutdownSteps(deadline)
                assert {step.name for step in pending} == {"runtime:score_wave", "profile:v5"}
                assert all(not step.completed and not step.timed_out for step in pending)
                assert PendingShutdownSteps(ShutdownDeadline.start(1)) == ()
                raise RuntimeError("controlled")
        assert tuple(step.name for step in PendingShutdownSteps(deadline)) == ("runtime:score_wave",)
    finally:
        release.set()
        worker.join(3)
        assert not worker.is_alive()
    assert PendingShutdownSteps(deadline) == ()


def test_watchdog_forced_exit_does_not_wait_for_a_blocked_logging_handler():
    entered, release, forced = threading.Event(), threading.Event(), threading.Event()
    exits, errors = [], []
    logger = logging.getLogger("trader.infra.shutdown")

    class BlockingHandler(logging.Handler):
        def emit(self, _record):
            entered.set()
            assert release.wait(3), "controlled log handler was not released"

    handler = BlockingHandler()
    logger.addHandler(handler)

    def hold_handler():
        try:
            logger.error("controlled logging blocker")
        except BaseException as exc:
            errors.append(exc)

    def fake_exit(code):
        exits.append(code)
        forced.set()

    holder = threading.Thread(target=hold_handler, name="test-shutdown-log-holder", daemon=True)
    watchdog = None
    holder.start()
    try:
        assert entered.wait(1)
        deadline = ShutdownDeadline.start(0.02)
        controller = ShutdownSignalController(
            timeout_seconds=0.02, on_first_signal=lambda _deadline: None, force_exit=fake_exit,
        )
        with ObserveShutdownStep(deadline, "profile:v5"), ObserveShutdownStep(deadline, "runtime:score_wave"):
            watchdog = threading.Thread(
                target=controller._force_when_expired,
                args=(deadline,),
                name="test-shutdown-watchdog",
                daemon=True,
            )
            watchdog.start()
            assert forced.wait(0.3), "blocked logging delayed the original watchdog deadline"
            assert exits == [2] and deadline.expired
            assert not release.is_set()
            assert all(step.timed_out for step in PendingShutdownSteps(deadline))
        assert PendingShutdownSteps(deadline) == ()
    finally:
        release.set()
        holder.join(3)
        if watchdog is not None:
            watchdog.join(3)
        logger.removeHandler(handler)
        handler.close()
        assert not holder.is_alive()
        assert watchdog is None or not watchdog.is_alive()
        assert errors == []


def test_shutdown_diagnostic_capacity_and_names_are_bounded():
    deadline = ShutdownDeadline.start(1)
    with ExitStack() as scopes:
        for index in range(66):
            scopes.enter_context(ObserveShutdownStep(deadline, f"runtime:{index}:" + "x" * 200))
        pending = PendingShutdownSteps(deadline)
        assert len(pending) == 65
        assert pending[-1].name == "shutdown:diagnostic_capacity"
        assert all(len(step.name) <= 160 for step in pending)
    assert PendingShutdownSteps(deadline) == ()


def test_shutdown_timeline_keeps_completed_owner_durations_and_is_bounded():
    now = [10.0]
    deadline = ShutdownDeadline.start(2, monotonic=lambda: now[0])
    with ObserveShutdownStep(deadline, "web"):
        now[0] = 10.5
        RecordShutdownEvent(deadline, "wakeup")
    spans = ReadShutdownTimeline(deadline)
    assert [(span.name, span.started_seconds, span.finished_seconds) for span in spans] == [
        ("web", 0.0, 0.5), ("wakeup", 0.5, 0.5),
    ]
    assert PendingShutdownSteps(deadline) == ()
    for _index in range(300):
        RecordShutdownEvent(deadline, "bounded")
    assert len(ReadShutdownTimeline(deadline)) == 256


@pytest.mark.parametrize("protect_process_exit,expected_code", ((False, 0), (True, 2)))
def test_server_watchdog_covers_interpreter_thread_join_after_cleanup_returns(protect_process_exit, expected_code):
    # This is an actual interpreter-exit tail, not a fake stop() wait. The
    # scoped CLI mode must still cancel its watchdog; server mode must not.
    code = """
import signal, threading
from trader.infra.shutdown import ShutdownSignalController
controller = ShutdownSignalController(timeout_seconds=0.15, on_first_signal=lambda deadline: None,
    protect_process_exit=PROTECT)
threading.Thread(target=lambda: threading.Event().wait(0.5), name='controlled-exit-tail').start()
controller.handle(signal.SIGTERM)
controller.mark_completed()
""".replace("PROTECT", repr(protect_process_exit))
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    completed = subprocess.run((sys.executable, "-c", code), env=environment, timeout=5, check=False)
    assert completed.returncode == expected_code


def test_final_shutdown_projection_does_not_hold_up_the_caller(monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def blocked_log(*_args, **_kwargs):
        entered.set()
        try:
            assert release.wait(3), "controlled report projection was not released"
        finally:
            finished.set()

    monkeypatch.setattr(server_entrypoint._LOGGER, "warning", blocked_log)
    deadline = ShutdownDeadline.start(1)
    report = ShutdownReport.from_steps(deadline, (ShutdownStep("application", True, False),))
    try:
        server_entrypoint._queue_shutdown_report(report, deadline)
        assert entered.wait(1)
        assert not finished.is_set()
    finally:
        release.set()
        assert finished.wait(3)
