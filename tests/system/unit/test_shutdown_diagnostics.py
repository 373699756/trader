from __future__ import annotations

import threading
import logging
from contextlib import ExitStack
from types import SimpleNamespace

import pytest

from tests.helpers.recommendation.runtime.scheduler import _round_cleanup_runtime
from trader.entrypoints import server as server_entrypoint
from trader.infra.shutdown import (
    ObserveShutdownStep,
    PendingShutdownSteps,
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
