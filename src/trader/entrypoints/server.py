"""HTTP server process entry point."""

from __future__ import annotations

import argparse
import inspect
import ipaddress
import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Protocol, cast

from flask import Flask
from werkzeug.serving import BaseWSGIServer, make_server

from trader.application.shutdown import (
    ShutdownDeadline,
    ShutdownReport,
    ShutdownSignalController,
    ShutdownStep,
)
from trader.bootstrap import build_system
from trader.infra.process_lock import ProcessLock, ProcessLockError
from trader.infra.settings import RuntimeSettings

_LOGGER = logging.getLogger(__name__)


class _StoppableSystem(Protocol):
    def stop(self) -> object: ...


class _RuntimeSystem(_StoppableSystem, Protocol):
    @property
    def settings(self) -> RuntimeSettings: ...

    @property
    def app(self) -> Flask: ...

    def start(self) -> bool: ...


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trader-server")
    parser.add_argument("--config", default=os.environ.get("TRADER_CONFIG", ""))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = _absolute_config_path(args.config)
    system = build_system(config_path)
    _validate_bind(system.settings)
    try:
        lock = ProcessLock(system.settings.runtime_dir / "server.lock")
        lock.acquire()
    except ProcessLockError as exc:
        raise SystemExit(str(exc)) from None
    with lock:
        return _run_system(
            system,
            timeout_seconds=system.settings.pipeline.shutdown_timeout_seconds,
        )


def _run_system(
    system: _RuntimeSystem,
    *,
    timeout_seconds: float,
) -> int:
    shutdown_requested = threading.Event()

    def request_shutdown(_deadline: ShutdownDeadline) -> None:
        shutdown_requested.set()

    controller = ShutdownSignalController(
        timeout_seconds=timeout_seconds,
        on_first_signal=request_shutdown,
    )
    started = False
    delegated = False
    try:
        controller.install()
        started = system.start()
        if shutdown_requested.is_set():
            deadline = controller.deadline or ShutdownDeadline.start(timeout_seconds)
            if started:
                report = _stop_system(system, deadline)
                if not report.completed:
                    _log_shutdown_report(report)
                    os._exit(2)
            controller.mark_completed()
            return controller.exit_code
        if not started:
            controller.mark_completed()
            return 0
        server = make_server(
            system.settings.server.host,
            system.settings.server.port,
            system.app,
            threaded=True,
        )
        print(
            f"{system.settings.server.host}:{system.settings.server.port}",
            flush=True,
        )
        delegated = True
        return _serve_with_controller(
            server,
            system,
            controller=controller,
            shutdown_requested=shutdown_requested,
            timeout_seconds=timeout_seconds,
        )
    except BaseException:
        if started and not delegated:
            deadline = controller.deadline or ShutdownDeadline.start(timeout_seconds)
            report = _stop_system(system, deadline)
            if not report.completed:
                _log_shutdown_report(report)
                os._exit(2)
            controller.mark_completed()
        raise
    finally:
        controller.restore()


def _serve_until_signal(
    server: BaseWSGIServer,
    system: _StoppableSystem,
    *,
    timeout_seconds: float,
) -> int:
    shutdown_requested = threading.Event()

    def request_shutdown(_deadline: ShutdownDeadline) -> None:
        shutdown_requested.set()

    controller = ShutdownSignalController(
        timeout_seconds=timeout_seconds,
        on_first_signal=request_shutdown,
    )
    try:
        controller.install()
        return _serve_with_controller(
            server,
            system,
            controller=controller,
            shutdown_requested=shutdown_requested,
            timeout_seconds=timeout_seconds,
        )
    finally:
        controller.restore()


def _serve_with_controller(
    server: BaseWSGIServer,
    system: _StoppableSystem,
    *,
    controller: ShutdownSignalController,
    shutdown_requested: threading.Event,
    timeout_seconds: float,
) -> int:
    server_failed = threading.Event()

    def serve() -> None:
        try:
            server.serve_forever()
        except BaseException:
            _LOGGER.exception("web server terminated unexpectedly")
            server_failed.set()
            shutdown_requested.set()

    try:
        web_thread = threading.Thread(target=serve, name="trader-web", daemon=False)
        web_thread.start()
        shutdown_requested.wait()
        deadline = controller.deadline or ShutdownDeadline.start(timeout_seconds)
        steps = [_stop_web(server, web_thread, deadline)]
        system_report = _stop_system(system, deadline)
        steps.extend(system_report.steps)
        report = ShutdownReport.from_steps(deadline, steps, forced=deadline.expired)
        if not report.completed:
            _log_shutdown_report(report)
            os._exit(2)
        controller.mark_completed()
        if server_failed.is_set():
            return 2
        return controller.exit_code
    except BaseException:
        deadline = controller.deadline or ShutdownDeadline.start(timeout_seconds)
        report = _stop_system(system, deadline)
        if not report.completed:
            _log_shutdown_report(report)
            os._exit(2)
        controller.mark_completed()
        raise


def _stop_web(
    server: BaseWSGIServer,
    web_thread: threading.Thread,
    deadline: ShutdownDeadline,
) -> ShutdownStep:
    completed = threading.Event()
    errors: list[BaseException] = []

    def stop() -> None:
        try:
            server.shutdown()
            server.server_close()
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()

    stopper = threading.Thread(target=stop, name="trader-web-stop", daemon=True)
    stopper.start()
    stopped = completed.wait(deadline.remaining_seconds())
    if stopped:
        web_thread.join(deadline.remaining_seconds())
    finished = stopped and not web_thread.is_alive() and not errors
    detail = ""
    if errors:
        detail = f"web shutdown failed:{type(errors[0]).__name__}"
    elif not finished:
        detail = "web server remains active"
    return ShutdownStep(
        name="web",
        completed=finished,
        timed_out=not finished and deadline.expired,
        detail=detail,
    )


def _stop_system(system: _StoppableSystem, deadline: ShutdownDeadline) -> ShutdownReport:
    completed = threading.Event()
    result: list[object] = []
    errors: list[BaseException] = []

    def stop() -> None:
        try:
            stop_method = cast(Callable[..., object], system.stop)
            if "deadline" in inspect.signature(stop_method).parameters:
                result.append(stop_method(deadline=deadline))
            else:
                result.append(stop_method())
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()

    stopper = threading.Thread(target=stop, name="trader-system-stop", daemon=True)
    stopper.start()
    finished = completed.wait(deadline.remaining_seconds())
    if finished and result and isinstance(result[0], ShutdownReport):
        return result[0]
    detail = ""
    if errors:
        detail = f"system shutdown failed:{type(errors[0]).__name__}"
    elif not finished:
        detail = "system shutdown exceeded process deadline"
    step = ShutdownStep(
        name="application",
        completed=finished and not errors,
        timed_out=not finished,
        detail=detail,
    )
    return ShutdownReport.from_steps(deadline, (step,))


def _log_shutdown_report(report: ShutdownReport) -> None:
    _LOGGER.error(
        "shutdown deadline exceeded: %s",
        json.dumps(asdict(report), ensure_ascii=True, sort_keys=True),
    )


def _absolute_config_path(raw_path: str) -> Path:
    if not raw_path:
        raise SystemExit("--config or TRADER_CONFIG is required")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise SystemExit("configuration path must be absolute")
    return path.resolve()


def _validate_bind(settings: RuntimeSettings) -> None:
    host = settings.server.host.strip().lower()
    is_loopback = host == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
    if not is_loopback and not settings.server.allow_insecure_non_loopback:
        raise SystemExit("non-loopback bind requires allow_insecure_non_loopback=true")


if __name__ == "__main__":
    raise SystemExit(main())
