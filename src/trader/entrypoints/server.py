"""HTTP server process entry point."""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Generic, Protocol, TypeVar, cast

from flask import Flask
from werkzeug.serving import BaseWSGIServer, make_server

from trader.infra.process_lock import ProcessLock, ProcessLockError
from trader.infra.linux_process import ReadLinuxProcessIdentity
from trader.infra.settings import LoadRuntimeSettings, RuntimeSettings
from trader.infra.shutdown import (
    ObserveShutdownStep,
    PendingShutdownSteps,
    ReadShutdownTimeline,
    RecordShutdownEvent,
    ShutdownDeadline,
    ShutdownIncompleteError,
    ShutdownReport,
    ShutdownSignalController,
    ShutdownStep,
    ShutdownWatchdogStartError,
)
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS, ScoringProfileId
from trader.recommendation.domain.scoring.profile_selection import ParseProfileSelection

_LOGGER = logging.getLogger(__name__)
_StopResultT = TypeVar("_StopResultT")


@dataclass(frozen=True)
class _PendingShutdown(Generic[_StopResultT]):
    completed: threading.Event
    results: list[_StopResultT]
    errors: list[BaseException]


class _StoppableSystem(Protocol):
    def stop(self, *, deadline: ShutdownDeadline) -> ShutdownReport: ...


class _RuntimeSystem(_StoppableSystem, Protocol):
    @property
    def settings(self) -> RuntimeSettings: ...

    @property
    def app(self) -> Flask: ...

    def start(self) -> bool: ...


def BuildParser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trader-server")
    parser.add_argument("--config", default=os.environ.get("TRADER_CONFIG", ""))
    parser.add_argument(
        "profiles",
        nargs="?",
        type=ParseProfileSelection,
        default=SCORING_PROFILE_IDS,
        help="Comma-separated profiles to run together; defaults to all profiles.",
    )
    return parser


def Main(argv: list[str] | None = None) -> int:
    args = BuildParser().parse_args(argv)
    profiles = cast(tuple[ScoringProfileId, ...], args.profiles)
    config_path = _absolute_config_path(args.config)
    settings = LoadRuntimeSettings(config_path)
    _validate_bind(settings)
    try:
        lock = ProcessLock(settings.runtime_dir / "server.lock")
        lock.acquire()
    except ProcessLockError as exc:
        raise SystemExit(_running_service_message(exc, settings)) from None
    with lock:
        from trader.bootstrap import BuildProfiledSystem, BuildSystem, PrepareRuntimeDisk

        PrepareRuntimeDisk(settings, choose=True, report=False)
        system = (
            BuildProfiledSystem(config_path, profiles=profiles)
            if len(profiles) > 1
            else BuildSystem(config_path, scoring_profile=profiles[0])
        )
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
        if not controller.watchdog_started:
            # Returning would expose synchronous startup rollback without any
            # guard capable of ending a blocked resource cleanup.
            os._exit(2)
        _start_shutdown_diagnostics(_deadline)

    controller = ShutdownSignalController(
        timeout_seconds=timeout_seconds,
        on_first_signal=request_shutdown,
        protect_process_exit=True,
    )
    started = False
    delegated = False
    cleanup_started = False
    server: BaseWSGIServer | None = None
    try:
        controller.install()
        try:
            started = system.start()
        except ShutdownIncompleteError:
            # Startup owners already own rollback; incomplete cleanup must not
            # enter Python's thread joins, repeat stop(), or wait for diagnostic I/O.
            os._exit(2)
        if shutdown_requested.is_set():
            cleanup_started = True
            if started:
                _shutdown_resources(system, controller=controller)
            else:
                _begin_shutdown(controller)
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
            f"浏览器登录地址->{_browser_url(system.settings.server.host, system.settings.server.port)}",
            flush=True,
        )
        delegated = True
        return _serve_with_controller(
            server,
            system,
            controller=controller,
            shutdown_requested=shutdown_requested,
        )
    except ShutdownWatchdogStartError:
        os._exit(2)
    except BaseException:
        if started and not delegated and not cleanup_started:
            cleanup_started = True
            _shutdown_resources(system, controller=controller, server=server)
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
        if not controller.watchdog_started:
            os._exit(2)
        _start_shutdown_diagnostics(_deadline)

    controller = ShutdownSignalController(
        timeout_seconds=timeout_seconds,
        on_first_signal=request_shutdown,
        protect_process_exit=True,
    )
    try:
        controller.install()
        return _serve_with_controller(
            server,
            system,
            controller=controller,
            shutdown_requested=shutdown_requested,
        )
    finally:
        controller.restore()


def _serve_with_controller(
    server: BaseWSGIServer,
    system: _StoppableSystem,
    *,
    controller: ShutdownSignalController,
    shutdown_requested: threading.Event,
) -> int:
    server_failed = threading.Event()

    def serve() -> None:
        try:
            server.serve_forever()
        except BaseException:
            _begin_shutdown(controller)
            server_failed.set()
            shutdown_requested.set()
            try:
                _LOGGER.exception("web server terminated unexpectedly")
            except Exception:
                pass

    web_thread: threading.Thread | None = None
    try:
        web_thread = threading.Thread(target=serve, name="trader-web", daemon=False)
        web_thread.start()
        shutdown_requested.wait()
    except ShutdownWatchdogStartError:
        os._exit(2)
    except BaseException:
        _shutdown_resources(system, controller=controller, server=server, web_thread=web_thread)
        raise
    _shutdown_resources(system, controller=controller, server=server, web_thread=web_thread)
    return 2 if server_failed.is_set() else controller.exit_code


def _begin_shutdown(controller: ShutdownSignalController) -> ShutdownDeadline:
    try:
        return controller.begin_shutdown()
    except ShutdownWatchdogStartError:
        # Owned resources cannot safely enter cleanup or diagnostics without
        # protection against blocked I/O and the interpreter's thread joins.
        os._exit(2)


def _shutdown_resources(
    system: _StoppableSystem,
    *,
    controller: ShutdownSignalController,
    server: BaseWSGIServer | None = None,
    web_thread: threading.Thread | None = None,
) -> None:
    deadline = _begin_shutdown(controller)
    steps: list[ShutdownStep] = []
    forced = False
    web_stop: _PendingShutdown[None] | None = None
    system_stop: _PendingShutdown[ShutdownReport] | None = None
    try:
        system_stop = _start_system_stop(system, deadline)
    except BaseException as exc:
        steps.append(ShutdownStep(
            "application", False, deadline.expired, detail=f"system shutdown failed:{type(exc).__name__}",
        ))
    if server is not None:
        try:
            web_stop = _start_web_stop(server, web_thread, deadline)
        except BaseException as exc:
            steps.append(ShutdownStep("web", False, deadline.expired, detail=f"web shutdown failed:{type(exc).__name__}"))
    # Both owners receive stop before either wait can consume the shared budget.
    if web_stop is not None and server is not None:
        try:
            steps.append(_wait_web_stop(web_stop, web_thread, deadline))
        except BaseException as exc:
            steps.append(ShutdownStep("web", False, deadline.expired, detail=f"web shutdown failed:{type(exc).__name__}"))
    if system_stop is not None:
        try:
            report = _wait_system_stop(system_stop, deadline)
            steps.extend(report.steps)
            forced = report.forced
        except BaseException as exc:
            steps.append(ShutdownStep(
                "application", False, deadline.expired, detail=f"system shutdown failed:{type(exc).__name__}",
            ))
    report = ShutdownReport.from_steps(deadline, steps, forced=forced or deadline.expired)
    if not report.completed:
        # A report already means all owners were attempted. Diagnostic I/O must
        # neither delay hard exit nor cause the caller to repeat those attempts.
        os._exit(2)
    try:
        controller.mark_completed()
        RecordShutdownEvent(deadline, "return_ready")
        _queue_shutdown_report(report, deadline)
    except Exception:
        # Projection is best effort after resources have closed; its failure
        # must not replace the original service error or repeat cleanup.
        pass


def _start_web_stop(
    server: BaseWSGIServer,
    web_thread: threading.Thread | None,
    deadline: ShutdownDeadline,
) -> _PendingShutdown[None]:

    def stop() -> None:
        try:
            if web_thread is not None and web_thread.ident is not None:
                server.shutdown()
        finally:
            server.server_close()

    try:
        return _start_shutdown_task(deadline, "web:stopper", "trader-web-stop", stop)
    except RuntimeError:
        if web_thread is None or web_thread.ident is None:
            # A bound socket has no serving loop to shut down. Even when its
            # stopper cannot start, close it under the already armed guard.
            server.server_close()
        raise


def _wait_web_stop(
    pending: _PendingShutdown[None],
    web_thread: threading.Thread | None,
    deadline: ShutdownDeadline,
) -> ShutdownStep:
    with ObserveShutdownStep(deadline, "web:wait"):
        stopped = pending.completed.wait(deadline.remaining_seconds())
        if stopped and web_thread is not None and web_thread.ident is not None:
            web_thread.join(deadline.remaining_seconds())
    finished = stopped and (web_thread is None or not web_thread.is_alive()) and not pending.errors
    detail = ""
    if pending.errors:
        detail = f"web shutdown failed:{type(pending.errors[0]).__name__}"
    elif not finished:
        detail = "web server remains active"
    return ShutdownStep(
        name="web",
        completed=finished,
        timed_out=not finished and deadline.expired,
        detail=detail,
    )


def _stop_system(system: _StoppableSystem, deadline: ShutdownDeadline) -> ShutdownReport:
    return _wait_system_stop(_start_system_stop(system, deadline), deadline)


def _start_system_stop(system: _StoppableSystem, deadline: ShutdownDeadline) -> _PendingShutdown[ShutdownReport]:
    def stop() -> ShutdownReport:
        report = system.stop(deadline=deadline)
        if not isinstance(report, ShutdownReport):
            raise TypeError("system_stop_report_required")
        return report

    return _start_shutdown_task(deadline, "application:stopper", "trader-system-stop", stop)


def _wait_system_stop(pending: _PendingShutdown[ShutdownReport], deadline: ShutdownDeadline) -> ShutdownReport:
    with ObserveShutdownStep(deadline, "application:wait"):
        finished = pending.completed.wait(deadline.remaining_seconds())
    if finished and pending.results:
        return pending.results[0]
    detail = ""
    if pending.errors:
        detail = f"system shutdown failed:{type(pending.errors[0]).__name__}"
    elif not finished:
        detail = "system shutdown exceeded process deadline"
    step = ShutdownStep(
        name="application",
        completed=finished and bool(pending.results) and not pending.errors,
        timed_out=not finished,
        detail=detail,
    )
    return ShutdownReport.from_steps(deadline, (step, *PendingShutdownSteps(deadline)))


def _start_shutdown_task(
    deadline: ShutdownDeadline,
    step_name: str,
    thread_name: str,
    stop: Callable[[], _StopResultT],
) -> _PendingShutdown[_StopResultT]:
    pending: _PendingShutdown[_StopResultT] = _PendingShutdown(threading.Event(), [], [])

    def run() -> None:
        try:
            with ObserveShutdownStep(deadline, step_name):
                pending.results.append(stop())
        except BaseException as exc:
            pending.errors.append(exc)
        finally:
            pending.completed.set()

    threading.Thread(target=run, name=thread_name, daemon=True).start()
    return pending


def _start_shutdown_diagnostics(deadline: ShutdownDeadline) -> None:
    # Projection may encounter a blocked log handler. It owns no resource that
    # main or the hard-deadline watchdog must wait for.
    def project() -> None:
        for _index in range(8):
            spans = ReadShutdownTimeline(deadline)
            frames = sys._current_frames()
            threads = []
            for thread in threading.enumerate()[:128]:
                frame = frames.get(thread.ident) if thread.ident is not None else None
                stack = []
                for _depth in range(12):
                    if frame is None:
                        break
                    stack.append(f"{Path(frame.f_code.co_filename).name}:{frame.f_lineno}:{frame.f_code.co_name}")
                    frame = frame.f_back
                threads.append({"name": thread.name[:160], "daemon": thread.daemon, "stack": stack})
            frames.clear()
            frame = None
            _LOGGER.warning("shutdown progress: %s", json.dumps({
                "process_identity": _shutdown_process_identity(),
                "started_at_monotonic": deadline.started_at_monotonic,
                "elapsed_seconds": deadline.elapsed_seconds,
                "timeline": [asdict(span) for span in spans],
                "threads": threads,
            }, ensure_ascii=False, sort_keys=True))
            if deadline.expired:
                return
            threading.Event().wait(min(5.0, deadline.remaining_seconds()))

    try:
        threading.Thread(target=project, name="trader-shutdown-diagnostics", daemon=True).start()
    except RuntimeError:
        # The already armed guard remains authoritative when optional
        # diagnostics cannot start; do not replace the cancellation failure.
        pass


def _queue_shutdown_report(report: ShutdownReport, deadline: ShutdownDeadline) -> None:
    def project() -> None:
        timeline = ReadShutdownTimeline(deadline)
        _LOGGER.warning("shutdown completed: %s", json.dumps({
            "process_identity": _shutdown_process_identity(),
            "started_at_monotonic": deadline.started_at_monotonic,
            "report": asdict(report), "timeline": [asdict(span) for span in timeline],
        }, ensure_ascii=False, sort_keys=True))

    threading.Thread(target=project, name="trader-shutdown-report", daemon=True).start()


def _shutdown_process_identity() -> dict[str, object] | None:
    if sys.platform != "linux":
        return None
    try:
        identity = ReadLinuxProcessIdentity(os.getpid())
    except (OSError, ValueError):
        # Diagnostics cannot hold up shutdown; unavailable identity is never guessed.
        return None
    return None if identity is None else {
        "pid": identity.pid, "birth_ticks": identity.birth_ticks, "boot_id": identity.boot_id,
    }


def _absolute_config_path(raw_path: str) -> Path:
    if not raw_path:
        raise SystemExit("--config or TRADER_CONFIG is required")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise SystemExit("configuration path must be absolute")
    return path.resolve()


def _browser_url(host: str, port: int) -> str:
    try:
        is_ipv6 = ipaddress.ip_address(host).version == 6
    except ValueError:
        is_ipv6 = False
    url_host = f"[{host}]" if is_ipv6 else host
    return f"http://{url_host}:{port}"


def _running_service_message(exc: ProcessLockError, settings: RuntimeSettings) -> str:
    return "\n".join(
        (
            str(exc),
            f"现有服务地址->{_browser_url(settings.server.host, settings.server.port)}",
            "请在原启动终端按 Ctrl+C 正常停止后，再运行原启动命令（./run.sh 或 .\\run.ps1）；不要删除 server.lock。",
        )
    )


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
    raise SystemExit(Main())
