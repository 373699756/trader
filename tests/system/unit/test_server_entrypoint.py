from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from flask import Flask

from trader.entrypoints import server as server_entrypoint
from trader.infra.settings import RuntimeSettings
from trader.infra.shutdown import (
    ShutdownDeadline, ShutdownIncompleteError, ShutdownReport, ShutdownSignalController, ShutdownStep,
    ShutdownWatchdogStartError,
)


class _Controller:
    deadline = None

    def __init__(self, **_kwargs: object) -> None:
        pass

    def install(self) -> None:
        pass

    def restore(self) -> None:
        pass


class _System:
    def __init__(self, host: str, port: int) -> None:
        self.settings = cast(
            RuntimeSettings,
            SimpleNamespace(server=SimpleNamespace(host=host, port=port)),
        )
        self.app = Flask(__name__)

    def start(self) -> bool:
        return True

    def stop(self, *, deadline: ShutdownDeadline) -> ShutdownReport:
        return ShutdownReport.from_steps(deadline, (ShutdownStep("fixture", True, False),))


class _GuardController(_Controller):
    deadline: ShutdownDeadline | None

    def __init__(self, deadline: ShutdownDeadline | None = None) -> None:
        self.deadline = deadline
        self.watchdog_started = False
        self.exit_code = 0
        self.completed_count = 0
        self.restored_count = 0

    def begin_shutdown(self) -> ShutdownDeadline:
        if self.deadline is None:
            self.deadline = ShutdownDeadline.start(5)
        self.watchdog_started = True
        return self.deadline

    def mark_completed(self) -> None:
        self.completed_count += 1

    def restore(self) -> None:
        self.restored_count += 1


class _BoundServer:
    def __init__(self) -> None:
        self.serving = threading.Event()
        self.closed = threading.Event()
        self.shutdown_count = 0
        self.close_count = 0

    def serve_forever(self) -> None:
        self.serving.set()
        assert self.closed.wait(2), "controlled serving loop was not closed"

    def shutdown(self) -> None:
        self.shutdown_count += 1
        self.closed.set()

    def server_close(self) -> None:
        self.close_count += 1
        self.closed.set()


class _HardExit(BaseException):
    pass


def _subprocess_environment() -> dict[str, str]:
    root = Path(__file__).resolve().parents[3]
    return dict(os.environ, PYTHONPATH=os.pathsep.join((str(root / "src"), str(root))))


def _prepare_controller(
    monkeypatch: pytest.MonkeyPatch, deadline: ShutdownDeadline | None = None,
) -> _GuardController:
    controller = _GuardController(deadline)

    def unexpected_exit(code: int) -> None:
        pytest.fail(f"unexpected hard exit:{code}")

    monkeypatch.setattr(server_entrypoint, "ShutdownSignalController", lambda **_kwargs: controller)
    monkeypatch.setattr(server_entrypoint, "_queue_shutdown_report", lambda *_args: None)
    monkeypatch.setattr(server_entrypoint.os, "_exit", unexpected_exit)
    return controller


@pytest.mark.parametrize(
    ("host", "port", "expected_url"),
    (
        ("127.0.0.1", 5000, "http://127.0.0.1:5000"),
        ("::1", 5001, "http://[::1]:5001"),
    ),
)
def test_run_system_prints_clickable_url_before_web_serving(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    host: str,
    port: int,
    expected_url: str,
) -> None:
    system = _System(host, port)
    bound_server = object()
    observed_output: list[str] = []

    monkeypatch.setattr(server_entrypoint, "ShutdownSignalController", _Controller)
    monkeypatch.setattr(server_entrypoint, "make_server", lambda *_args, **_kwargs: bound_server)

    def serve(*_args: object, **_kwargs: object) -> int:
        observed_output.append(capsys.readouterr().out)
        return 0

    monkeypatch.setattr(server_entrypoint, "_serve_with_controller", serve)

    assert server_entrypoint._run_system(system, timeout_seconds=30.0) == 0
    assert observed_output == [f"浏览器登录地址->{expected_url}\n"]


def test_server_parser_accepts_every_configured_tomorrow_profile() -> None:
    for profile in ("v1", "v2", "v3", "v5"):
        assert server_entrypoint.BuildParser().parse_args([profile]).profiles == (profile,)


@pytest.mark.parametrize("signal_deadline_offset", (None, -1.0, 1.0))
def test_incomplete_startup_rollback_exits_without_repeating_cleanup_or_extending_budget(
    monkeypatch: pytest.MonkeyPatch, signal_deadline_offset: float | None,
) -> None:
    deadline = ShutdownDeadline(10.0, 30.0, monotonic=lambda: 11.0)
    report = ShutdownReport.from_steps(deadline, (ShutdownStep("fixture", False, False),))
    failure = ShutdownIncompleteError(report, deadline)
    restored: list[bool] = []
    exits: list[int] = []

    class HardExit(BaseException):
        pass

    class Controller(_Controller):
        def __init__(self, **_kwargs: object) -> None:
            self.deadline = None if signal_deadline_offset is None else ShutdownDeadline(
                10.0 + signal_deadline_offset, 30.0, monotonic=lambda: 11.0,
            )

        def restore(self) -> None:
            restored.append(True)

    class System(_System):
        def start(self) -> bool:
            raise failure

        def stop(self, *, deadline: ShutdownDeadline) -> ShutdownReport:
            pytest.fail("rollback owners must not be stopped twice")

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("incomplete rollback must not restart cleanup, serve or wait for diagnostics")

    def hard_exit(code: int) -> None:
        exits.append(code)
        raise HardExit()

    monkeypatch.setattr(server_entrypoint, "ShutdownSignalController", Controller)
    monkeypatch.setattr(server_entrypoint.os, "_exit", hard_exit)
    monkeypatch.setattr(server_entrypoint, "make_server", unexpected)
    monkeypatch.setattr(server_entrypoint._LOGGER, "error", unexpected)
    monkeypatch.setattr(server_entrypoint, "_queue_shutdown_report", unexpected)
    monkeypatch.setattr(server_entrypoint, "_start_shutdown_diagnostics", unexpected)
    monkeypatch.setattr(ShutdownDeadline, "start", unexpected)

    with pytest.raises(HardExit):
        server_entrypoint._run_system(System("127.0.0.1", 5000), timeout_seconds=30.0)

    assert exits == [2]
    assert restored == [True]


@pytest.mark.parametrize("failure", (RuntimeError("startup failed"), KeyboardInterrupt(), SystemExit(7)))
def test_startup_failure_after_complete_rollback_preserves_original_exception(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException,
) -> None:
    restored: list[bool] = []

    class Controller(_Controller):
        def restore(self) -> None:
            restored.append(True)

    class System(_System):
        def start(self) -> bool:
            raise failure

    def unexpected_exit(_code: int) -> None:
        pytest.fail("a fully rolled back startup must preserve its original exception")

    monkeypatch.setattr(server_entrypoint, "ShutdownSignalController", Controller)
    monkeypatch.setattr(server_entrypoint.os, "_exit", unexpected_exit)

    with pytest.raises(type(failure)) as caught:
        server_entrypoint._run_system(System("127.0.0.1", 5000), timeout_seconds=30.0)

    assert caught.value is failure
    assert restored == [True]


def test_incomplete_startup_rollback_does_not_wait_for_threads_or_blocked_logging() -> None:
    code = """
import logging
import threading
from trader.entrypoints import server
from trader.infra.shutdown import ShutdownDeadline, ShutdownIncompleteError, ShutdownReport, ShutdownStep

entered = threading.Event()
release = threading.Event()
class BlockedHandler(logging.Handler):
    def emit(self, record):
        entered.set()
        release.wait(10)
handler = BlockedHandler()
server._LOGGER.addHandler(handler)
threading.Thread(target=lambda: server._LOGGER.error('controlled blocker'), daemon=True).start()
assert entered.wait(1), 'controlled logging holder did not enter'

class System:
    def start(self):
        threading.Thread(target=lambda: release.wait(10), name='controlled-startup-tail').start()
        deadline = ShutdownDeadline.start(0.05)
        report = ShutdownReport.from_steps(deadline, (ShutdownStep('fixture', False, False),))
        raise ShutdownIncompleteError(report, deadline)
    def stop(self, *, deadline):
        raise AssertionError('rollback must not run twice')

server._run_system(System(), timeout_seconds=30)
"""
    completed = subprocess.run(
        (sys.executable, "-c", code), env=_subprocess_environment(), timeout=3, check=False, capture_output=True,
    )
    assert completed.returncode == 2, completed.stderr.decode(errors="replace")


@pytest.mark.parametrize("failure_stage", ("bind", "url"))
@pytest.mark.parametrize("failure", (RuntimeError("controlled failure"), KeyboardInterrupt(), SystemExit(7)))
@pytest.mark.parametrize("signalled", (False, True))
def test_pre_handoff_failure_closes_every_owned_resource_once_and_preserves_the_error(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    failure: BaseException,
    signalled: bool,
) -> None:
    original_deadline = ShutdownDeadline.start(5) if signalled else None
    controller = _prepare_controller(monkeypatch, original_deadline)
    bound = _BoundServer()
    received: list[ShutdownDeadline] = []

    class System(_System):
        def stop(self, *, deadline: ShutdownDeadline) -> ShutdownReport:
            assert controller.watchdog_started
            received.append(deadline)
            return super().stop(deadline=deadline)

    def bind(*_args: object, **_kwargs: object) -> _BoundServer:
        if failure_stage == "bind":
            raise failure
        return bound

    def output(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(server_entrypoint, "make_server", bind)
    monkeypatch.setattr(server_entrypoint, "print", output, raising=False)

    with pytest.raises(type(failure)) as caught:
        server_entrypoint._run_system(System("127.0.0.1", 5000), timeout_seconds=30)

    assert caught.value is failure
    assert received == [controller.deadline]
    if original_deadline is not None:
        assert received[0] is original_deadline
    assert bound.close_count == int(failure_stage == "url")
    assert bound.shutdown_count == 0
    assert controller.completed_count == controller.restored_count == 1


@pytest.mark.parametrize("failure", (RuntimeError("thread startup failed"), KeyboardInterrupt(), SystemExit(7)))
def test_web_thread_start_failure_only_closes_the_bound_socket_and_preserves_the_error(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException,
) -> None:
    controller = _prepare_controller(monkeypatch)
    bound = _BoundServer()
    received: list[ShutdownDeadline] = []
    start_thread = threading.Thread.start

    class System(_System):
        def stop(self, *, deadline: ShutdownDeadline) -> ShutdownReport:
            assert controller.watchdog_started
            received.append(deadline)
            return super().stop(deadline=deadline)

    def start(thread: threading.Thread) -> None:
        if thread.name == "trader-web":
            raise failure
        start_thread(thread)

    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(server_entrypoint, "make_server", lambda *_args, **_kwargs: bound)
    with pytest.raises(type(failure)) as caught:
        server_entrypoint._run_system(System("127.0.0.1", 5000), timeout_seconds=30)

    assert caught.value is failure
    assert received == [controller.deadline]
    assert bound.close_count == 1 and bound.shutdown_count == 0
    assert controller.completed_count == controller.restored_count == 1


def test_web_failure_arms_the_guard_and_notifies_before_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _prepare_controller(monkeypatch)
    received: list[ShutdownDeadline] = []
    notified = threading.Event()
    diagnosed = threading.Event()

    class Server(_BoundServer):
        def serve_forever(self) -> None:
            raise RuntimeError("controlled serving failure")

    class System(_System):
        def stop(self, *, deadline: ShutdownDeadline) -> ShutdownReport:
            assert controller.watchdog_started
            received.append(deadline)
            return super().stop(deadline=deadline)

    def log(*_args: object, **_kwargs: object) -> None:
        assert controller.watchdog_started and notified.is_set()
        diagnosed.set()
        raise RuntimeError("optional diagnostics failed")

    bound = Server()
    monkeypatch.setattr(server_entrypoint._LOGGER, "exception", log)
    result = server_entrypoint._serve_with_controller(
        bound, System("127.0.0.1", 5000), controller=controller, shutdown_requested=notified,
    )
    assert result == 2
    assert diagnosed.is_set()
    assert received == [controller.deadline]
    assert bound.shutdown_count == bound.close_count == 1
    assert controller.completed_count == 1


@pytest.mark.parametrize("failed_thread", ("trader-system-stop", "trader-web-stop"))
def test_stopper_start_failure_attempts_the_other_owner_and_preserves_failure(
    monkeypatch: pytest.MonkeyPatch, failed_thread: str,
) -> None:
    controller = _prepare_controller(monkeypatch)
    bound = _BoundServer()
    received: list[ShutdownDeadline] = []
    attempts: list[str] = []
    start_thread = threading.Thread.start
    forced: list[int] = []

    class System(_System):
        def stop(self, *, deadline: ShutdownDeadline) -> ShutdownReport:
            received.append(deadline)
            return super().stop(deadline=deadline)

    def start(thread: threading.Thread) -> None:
        attempts.append(thread.name)
        if thread.name == failed_thread:
            raise RuntimeError("controlled stopper startup failure")
        start_thread(thread)

    def hard_exit(code: int) -> None:
        forced.append(code)
        raise _HardExit()

    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(server_entrypoint.os, "_exit", hard_exit)
    with pytest.raises(_HardExit):
        server_entrypoint._shutdown_resources(
            System("127.0.0.1", 5000), controller=controller, server=bound,
        )

    assert controller.watchdog_started and forced == [2]
    assert attempts == ["trader-system-stop", "trader-web-stop"]
    assert received == ([] if failed_thread == "trader-system-stop" else [controller.deadline])
    assert bound.close_count == 1 and bound.shutdown_count == 0
    assert controller.completed_count == 0


@pytest.mark.parametrize("failure", (RuntimeError("web cleanup failed"), KeyboardInterrupt(), SystemExit(7)))
def test_web_stop_error_still_closes_socket_and_stops_application_once(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException,
) -> None:
    controller = _prepare_controller(monkeypatch)
    received: list[ShutdownDeadline] = []
    forced: list[int] = []

    class Server(_BoundServer):
        def serve_forever(self) -> None:
            raise RuntimeError("controlled serving failure")

        def shutdown(self) -> None:
            self.shutdown_count += 1
            raise failure

    class System(_System):
        def stop(self, *, deadline: ShutdownDeadline) -> ShutdownReport:
            received.append(deadline)
            return super().stop(deadline=deadline)

    def hard_exit(code: int) -> None:
        forced.append(code)
        raise _HardExit()

    bound = Server()
    monkeypatch.setattr(server_entrypoint.os, "_exit", hard_exit)
    with pytest.raises(_HardExit):
        server_entrypoint._serve_with_controller(
            bound, System("127.0.0.1", 5000), controller=controller, shutdown_requested=threading.Event(),
        )

    assert received == [controller.deadline] and forced == [2]
    assert bound.shutdown_count == bound.close_count == 1


def test_successful_web_shutdown_keeps_the_existing_deadline_and_signal_code(monkeypatch: pytest.MonkeyPatch) -> None:
    deadline = ShutdownDeadline.start(5)
    controller = _prepare_controller(monkeypatch, deadline)
    controller.exit_code = 143
    requested = threading.Event()
    requested.set()
    received: list[ShutdownDeadline] = []
    bound = _BoundServer()

    class System(_System):
        def stop(self, *, deadline: ShutdownDeadline) -> ShutdownReport:
            received.append(deadline)
            return super().stop(deadline=deadline)

    assert server_entrypoint._serve_with_controller(
        bound, System("127.0.0.1", 5000), controller=controller, shutdown_requested=requested,
    ) == 143
    assert received == [deadline]
    assert bound.shutdown_count == bound.close_count == controller.completed_count == 1


@pytest.mark.parametrize("signal_timing", ("startup", "cleanup"))
def test_real_signal_during_startup_or_exception_cleanup_reuses_the_first_budget(
    monkeypatch: pytest.MonkeyPatch, signal_timing: str,
) -> None:
    controllers: list[ShutdownSignalController] = []
    forced: list[int] = []
    received: list[ShutdownDeadline] = []
    observed: list[ShutdownDeadline] = []
    notified = threading.Event()
    failure = RuntimeError("controlled binding failure")

    def create_controller(**kwargs: object) -> ShutdownSignalController:
        # Scoped mode cancels this unit test's daemon; subprocess tests exercise
        # the server's real protection through interpreter exit.
        controller = ShutdownSignalController(
            timeout_seconds=5,
            on_first_signal=cast(Callable[[ShutdownDeadline], None], kwargs["on_first_signal"]),
            force_exit=forced.append,
        )
        controllers.append(controller)
        return controller

    class System(_System):
        def start(self) -> bool:
            if signal_timing == "startup":
                signal.raise_signal(signal.SIGTERM)
            return True

        def stop(self, *, deadline: ShutdownDeadline) -> ShutdownReport:
            received.append(deadline)
            if signal_timing == "cleanup":
                os.kill(os.getpid(), signal.SIGTERM)
                assert notified.wait(1), "controlled signal was not handled before cleanup returned"
            return super().stop(deadline=deadline)

    def bind(*_args: object, **_kwargs: object) -> None:
        raise failure

    def diagnose(deadline: ShutdownDeadline) -> None:
        observed.append(deadline)
        notified.set()

    def unexpected_exit(code: int) -> None:
        pytest.fail(f"unexpected hard exit:{code}")

    monkeypatch.setattr(server_entrypoint, "ShutdownSignalController", create_controller)
    monkeypatch.setattr(server_entrypoint, "_start_shutdown_diagnostics", diagnose)
    monkeypatch.setattr(server_entrypoint, "_queue_shutdown_report", lambda *_args: None)
    monkeypatch.setattr(server_entrypoint, "make_server", bind)
    monkeypatch.setattr(server_entrypoint.os, "_exit", unexpected_exit)
    if signal_timing == "startup":
        assert server_entrypoint._run_system(System("127.0.0.1", 5000), timeout_seconds=30) == 143
    else:
        with pytest.raises(RuntimeError) as caught:
            server_entrypoint._run_system(System("127.0.0.1", 5000), timeout_seconds=30)
        assert caught.value is failure
    controller = controllers[0]
    assert received == observed == [controller.deadline]
    assert forced == []
    controller.handle(signal.SIGTERM)
    assert forced == [143]


@pytest.mark.parametrize("projection_failure", ("controller", "timeline", "report"))
def test_completed_cleanup_diagnostics_cannot_replace_the_original_error_or_repeat_stop(
    monkeypatch: pytest.MonkeyPatch, projection_failure: str,
) -> None:
    controller = _prepare_controller(monkeypatch)
    failure = SystemExit(7)
    received: list[ShutdownDeadline] = []

    class System(_System):
        def stop(self, *, deadline: ShutdownDeadline) -> ShutdownReport:
            received.append(deadline)
            return super().stop(deadline=deadline)

    def bind(*_args: object, **_kwargs: object) -> None:
        raise failure

    def project(*_args: object) -> None:
        raise RuntimeError("controlled projection failure")

    monkeypatch.setattr(server_entrypoint, "make_server", bind)
    if projection_failure == "controller":
        monkeypatch.setattr(controller, "mark_completed", project)
    elif projection_failure == "timeline":
        monkeypatch.setattr(server_entrypoint, "RecordShutdownEvent", project)
    else:
        monkeypatch.setattr(server_entrypoint, "_queue_shutdown_report", project)
    with pytest.raises(SystemExit) as caught:
        server_entrypoint._run_system(System("127.0.0.1", 5000), timeout_seconds=30)
    assert caught.value is failure
    assert received == [controller.deadline]
    assert controller.completed_count == int(projection_failure != "controller")
    assert controller.restored_count == 1


def test_watchdog_start_failure_forces_exit_without_cleanup_or_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = _prepare_controller(monkeypatch)
    forced: list[int] = []
    deadline = ShutdownDeadline.start(0.1)

    def fail_guard() -> ShutdownDeadline:
        raise ShutdownWatchdogStartError(deadline)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("unprotected cleanup or diagnostics must not delay forced failure")

    def bind(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("controlled binding failure")

    def hard_exit(code: int) -> None:
        forced.append(code)
        raise _HardExit()

    monkeypatch.setattr(controller, "begin_shutdown", fail_guard)
    monkeypatch.setattr(server_entrypoint, "make_server", bind)
    monkeypatch.setattr(server_entrypoint, "_start_system_stop", forbidden)
    monkeypatch.setattr(server_entrypoint, "_queue_shutdown_report", forbidden)
    monkeypatch.setattr(server_entrypoint.os, "_exit", hard_exit)
    with pytest.raises(_HardExit):
        server_entrypoint._run_system(_System("127.0.0.1", 5000), timeout_seconds=30)
    assert forced == [2] and controller.restored_count == 1


@pytest.mark.parametrize("failure_stage", ("startup", "bind", "web_start"))
def test_signal_watchdog_failure_exits_without_repeating_owned_cleanup(
    monkeypatch: pytest.MonkeyPatch, failure_stage: str,
) -> None:
    controller = _prepare_controller(monkeypatch)
    failure = ShutdownWatchdogStartError(ShutdownDeadline.start(0.1))
    forced: list[int] = []
    start_thread = threading.Thread.start

    class System(_System):
        def start(self) -> bool:
            if failure_stage == "startup":
                raise failure
            return True

        def stop(self, *, deadline: ShutdownDeadline) -> ShutdownReport:
            pytest.fail("startup owners already own rollback")

    def hard_exit(code: int) -> None:
        forced.append(code)
        raise _HardExit()

    def bind(*_args: object, **_kwargs: object) -> _BoundServer:
        if failure_stage == "bind":
            raise failure
        return _BoundServer()

    def start(thread: threading.Thread) -> None:
        if thread.name == "trader-web":
            raise failure
        start_thread(thread)

    monkeypatch.setattr(server_entrypoint.os, "_exit", hard_exit)
    monkeypatch.setattr(server_entrypoint, "make_server", bind)
    monkeypatch.setattr(threading.Thread, "start", start)
    with pytest.raises(_HardExit):
        server_entrypoint._run_system(System("127.0.0.1", 5000), timeout_seconds=30)
    assert forced == [2] and controller.restored_count == 1


def test_real_signal_watchdog_start_failure_is_not_masked_by_diagnostic_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controllers: list[ShutdownSignalController] = []
    attempts: list[str] = []
    forced: list[int] = []
    failure = RuntimeError("controlled thread startup failure")

    def create_controller(
        *,
        timeout_seconds: float,
        on_first_signal: Callable[[ShutdownDeadline], None],
        protect_process_exit: bool,
    ) -> ShutdownSignalController:
        controller = ShutdownSignalController(
            timeout_seconds=timeout_seconds,
            on_first_signal=on_first_signal,
            protect_process_exit=protect_process_exit,
            force_exit=forced.append,
        )
        controllers.append(controller)
        return controller

    class System(_System):
        def start(self) -> bool:
            signal.raise_signal(signal.SIGTERM)
            pytest.fail("the failed startup watchdog must propagate")

        def stop(self, *, deadline: ShutdownDeadline) -> ShutdownReport:
            pytest.fail("startup owners already own rollback")

    def start(thread: threading.Thread) -> None:
        attempts.append(thread.name)
        raise failure

    def hard_exit(code: int) -> None:
        forced.append(code)
        raise _HardExit()

    monkeypatch.setattr(server_entrypoint, "ShutdownSignalController", create_controller)
    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(server_entrypoint.os, "_exit", hard_exit)
    with pytest.raises(_HardExit):
        server_entrypoint._run_system(System("127.0.0.1", 5000), timeout_seconds=30)

    assert forced == [2]
    assert attempts == ["trader-shutdown-watchdog"]
    controller = controllers[0]
    assert not controller.watchdog_started
    assert controller.watchdog_failure is not None
    assert controller.watchdog_failure.__cause__ is failure


def test_optional_diagnostic_thread_start_failure_does_not_replace_the_shutdown_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []

    def start(thread: threading.Thread) -> None:
        attempts.append(thread.name)
        raise RuntimeError("controlled diagnostic startup failure")

    monkeypatch.setattr(threading.Thread, "start", start)
    server_entrypoint._start_shutdown_diagnostics(ShutdownDeadline.start(0.1))
    assert attempts == ["trader-shutdown-diagnostics"]


def test_failed_signal_watchdog_exits_before_entering_blocking_startup_rollback() -> None:
    code = """
import os, signal, threading
from trader.entrypoints import server

start_thread = threading.Thread.start
def start(thread):
    if thread.name == 'trader-shutdown-watchdog':
        raise RuntimeError('controlled watchdog startup failure')
    return start_thread(thread)
threading.Thread.start = start

class System:
    def start(self):
        try:
            signal.raise_signal(signal.SIGTERM)
        finally:
            os.write(1, b'rollback_entered\\n')
            threading.Event().wait(10)
    def stop(self, *, deadline):
        os.write(1, b'outer_stop_entered\\n')
        threading.Event().wait(10)

server._run_system(System(), timeout_seconds=0.15)
"""
    completed = subprocess.run(
        (sys.executable, "-c", code), env=_subprocess_environment(), timeout=3, check=False, capture_output=True,
    )
    assert completed.returncode == 2, completed.stderr.decode(errors="replace")
    assert b"rollback_entered" not in completed.stdout
    assert b"outer_stop_entered" not in completed.stdout


@pytest.mark.parametrize("scenario", ("bind", "url", "web", "web_start", "signal"))
def test_service_failures_leave_no_unbounded_exit_tail_with_blocked_logging(scenario: str) -> None:
    code = f"SCENARIO = {scenario!r}\n" + """
import logging, os, signal, threading
from types import SimpleNamespace
from trader.entrypoints import server
from trader.infra.shutdown import ShutdownReport, ShutdownStep

entered = threading.Event()
release = threading.Event()
class BlockedHandler(logging.Handler):
    def emit(self, record):
        entered.set()
        release.wait(10)
server._LOGGER.addHandler(BlockedHandler())
threading.Thread(target=lambda: server._LOGGER.error('controlled blocker'), daemon=True).start()
assert entered.wait(1), 'controlled logging holder did not enter'

controllers = []
original_deadline = []
real_controller = server.ShutdownSignalController
def create_controller(**kwargs):
    controller = real_controller(**kwargs)
    controllers.append(controller)
    return controller
server.ShutdownSignalController = create_controller

class System:
    settings = SimpleNamespace(server=SimpleNamespace(host='127.0.0.1', port=5000))
    app = None
    def start(self):
        threading.Thread(target=lambda: release.wait(10), name='controlled-exit-tail').start()
        if SCENARIO == 'signal':
            signal.raise_signal(signal.SIGTERM)
            original_deadline.append(controllers[0].deadline)
        return True
    def stop(self, *, deadline):
        if original_deadline:
            assert deadline is original_deadline[0], 'signal deadline was replaced'
        os.write(1, b'system_stop\\n')
        return ShutdownReport.from_steps(deadline, (ShutdownStep('fixture', True, False),))

class BoundServer:
    def serve_forever(self):
        raise RuntimeError('controlled serving failure')
    def shutdown(self):
        os.write(1, b'web_shutdown\\n')
    def server_close(self):
        os.write(1, b'socket_closed\\n')

def bind(*args, **kwargs):
    if SCENARIO == 'bind':
        raise RuntimeError('controlled binding failure')
    return BoundServer()
server.make_server = bind
if SCENARIO == 'url':
    def output(*args, **kwargs):
        raise RuntimeError('controlled output failure')
    server.print = output
if SCENARIO == 'web_start':
    start_thread = threading.Thread.start
    def start(thread):
        if thread.name == 'trader-web':
            raise RuntimeError('controlled thread startup failure')
        return start_thread(thread)
    threading.Thread.start = start

server._run_system(System(), timeout_seconds=0.15)
"""
    completed = subprocess.run(
        (sys.executable, "-c", code), env=_subprocess_environment(), timeout=3, check=False, capture_output=True,
    )
    assert completed.returncode == 2, completed.stderr.decode(errors="replace")
    assert completed.stdout.count(b"system_stop\n") == 1
    if scenario in {"url", "web", "web_start"}:
        assert completed.stdout.count(b"socket_closed\n") == 1
    if scenario in {"url", "web_start"}:
        assert b"web_shutdown\n" not in completed.stdout
