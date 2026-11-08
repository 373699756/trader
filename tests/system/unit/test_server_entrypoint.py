from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace
from typing import cast

import pytest
from flask import Flask

from trader.entrypoints import server as server_entrypoint
from trader.infra.settings import RuntimeSettings
from trader.infra.shutdown import ShutdownDeadline, ShutdownIncompleteError, ShutdownReport, ShutdownStep


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
    monkeypatch.setattr(server_entrypoint, "_log_shutdown_report", unexpected)
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
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    completed = subprocess.run(
        (sys.executable, "-c", code), env=environment, timeout=3, check=False, capture_output=True,
    )
    assert completed.returncode == 2, completed.stderr.decode(errors="replace")
