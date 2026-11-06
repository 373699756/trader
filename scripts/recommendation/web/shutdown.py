"""Observe one explicitly identified Linux process; SIGTERM requires --send-sigterm."""

from __future__ import annotations

import argparse
import errno
import json
import math
import os
import re
import select
import signal
import stat
import sys
import time
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from scripts.core.contracts import CheckStatus, ToolOutcome
from trader.infra.linux_process import (
    LinuxProcessCapabilityError, LinuxProcessIdentity,
    OpenLinuxProcessDescriptor, ReadLinuxProcessIdentity, SignalLinuxProcessDescriptor,
)

_LOG_LIMIT = 1024 * 1024
_SHUTDOWN_LIMIT_SECONDS = 30.0


@dataclass(frozen=True)
class ShutdownObservationOptions:
    pid: int
    birth_ticks: int
    shutdown_log: Path
    observation_seconds: float = 40.0
    send_sigterm: bool = False

    def __post_init__(self) -> None:
        if type(self.pid) is not int or self.pid <= 0 or type(self.birth_ticks) is not int or self.birth_ticks < 0:
            raise ValueError("shutdown_observation_identity_invalid")
        if not math.isfinite(self.observation_seconds) or not 0 < self.observation_seconds <= 60:
            raise ValueError("shutdown_observation_duration_invalid")


@dataclass(frozen=True)
class _LogObservation:
    handler_started: float | None = None
    return_ready: float | None = None
    resource_completed: bool | None = None
    resource_forced: bool | None = None
    matched_records: int = 0
    bytes_read: int = 0
    truncated: bool = False
    conflicting: bool = False
    log_device: int | None = None
    log_inode: int | None = None
    log_start_offset: int = 0


@dataclass(frozen=True)
class _Times:
    observation_started: float
    signal_send_started: float | None
    signal_send_finished: float | None
    pidfd_wait_started: float
    pidfd_wait_finished: float
    exit_observed: float | None


class ShutdownObservationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def BuildParser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--pid", required=True, type=int, help="exact target PID; never a process name or group")
    parser.add_argument("--birth-ticks", required=True, type=int, help="expected /proc PID stat birth identity")
    parser.add_argument("--shutdown-log", required=True, type=Path, help="existing regular stderr/log file containing this server's shutdown events")
    parser.add_argument("--observation-seconds", type=float, default=40.0, help="bounded pidfd observation, at most 60 seconds; does not change the 30-second gate")
    parser.add_argument("--send-sigterm", action="store_true", help="explicitly send one SIGTERM to this identity; default only observes and never signals or force-kills it")
    return parser


def Run(action_identity: str, arguments: list[str]) -> ToolOutcome:
    if action_identity != "shutdown":
        raise ValueError("unsupported shutdown observation action")
    parser = BuildParser()
    args = parser.parse_args(arguments)
    if args.pid <= 0 or args.birth_ticks < 0:
        parser.error("--pid must be positive and --birth-ticks nonnegative")
    if not math.isfinite(args.observation_seconds) or not 0 < args.observation_seconds <= 60:
        parser.error("--observation-seconds must be within (0, 60]")
    options = ShutdownObservationOptions(
        args.pid, args.birth_ticks, args.shutdown_log.expanduser().resolve(),
        args.observation_seconds, args.send_sigterm,
    )
    try:
        return ObserveShutdown(options)
    except ShutdownObservationError as exc:
        return _failure(options, exc.code)
    except LinuxProcessCapabilityError as exc:
        return _failure(options, str(exc), category="unsupported")
    except OSError as exc:
        category = {
            errno.ENOSYS: "unsupported", errno.EPERM: "permission_denied", errno.EACCES: "permission_denied",
            errno.ESRCH: "target_exited",
        }.get(exc.errno, "os_error") if exc.errno is not None else "os_error"
        return _failure(options, "shutdown_observation_" + category, category=category, error_number=exc.errno)


def _failure(
    options: ShutdownObservationOptions, code: str, *, category: str = "invalid_observation",
    error_number: int | None = None,
) -> ToolOutcome:
    return ToolOutcome(1, {
        "schema_version": "shutdown-observation", "status": "failed",
        "summary": {"read_only": not options.send_sigterm, "runtime_shutdown_acceptance": False,
                    "failure_category": category, "failure_errno": error_number},
        "findings": [code],
    })


def ObserveShutdown(options: ShutdownObservationOptions) -> ToolOutcome:
    """Observe a pinned pidfd; own descriptors only, never take ownership of the target."""
    if sys.platform != "linux":
        raise ShutdownObservationError("shutdown_observation_linux_required")
    if options.pid == os.getpid():
        raise ShutdownObservationError("shutdown_observation_self_target_rejected")
    identity = ReadLinuxProcessIdentity(options.pid)
    if identity is None or identity.birth_ticks != options.birth_ticks:
        raise ShutdownObservationError("shutdown_observation_target_identity_unavailable")
    with ExitStack() as resources:
        log_descriptor = os.open(options.shutdown_log, os.O_RDONLY | os.O_NONBLOCK)
        resources.callback(os.close, log_descriptor)
        if not stat.S_ISREG(os.fstat(log_descriptor).st_mode):
            raise ShutdownObservationError("shutdown_observation_regular_log_required")
        log = resources.enter_context(os.fdopen(log_descriptor, "rb", closefd=False))
        offset = log.seek(0, os.SEEK_END)
        pidfd = OpenLinuxProcessDescriptor(identity)
        resources.callback(os.close, pidfd)
        if ReadLinuxProcessIdentity(identity.pid) != identity:
            raise ShutdownObservationError("shutdown_observation_target_changed_before_wait")
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)
        if poller.poll(0):
            raise ShutdownObservationError("shutdown_observation_target_already_exited")
        started = time.monotonic()
        send_started = send_finished = exited = None
        cancelled = False
        wait_started = wait_finished = started
        try:
            if options.send_sigterm:
                send_started = time.monotonic()
                SignalLinuxProcessDescriptor(pidfd, signal.SIGTERM)
                send_finished = time.monotonic()
            wait_started = time.monotonic()
            remaining = max(0.0, options.observation_seconds - (wait_started - started))
            ready = poller.poll(math.ceil(remaining * 1000))
            wait_finished = time.monotonic()
            if ready:
                if any(events & (select.POLLIN | select.POLLHUP) for _descriptor, events in ready):
                    exited = wait_finished
                else:
                    raise ShutdownObservationError("shutdown_observation_pidfd_wait_invalid")
        except KeyboardInterrupt:
            cancelled = True
            wait_finished = time.monotonic()
        observed = _read_events(log, offset, identity, started)
        times = _Times(started, send_started, send_finished, wait_started, wait_finished, exited)
        return _outcome(options, identity, times, observed, cancelled=cancelled)


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _read_events(log: BinaryIO, offset: int, identity: LinuxProcessIdentity, started: float) -> _LogObservation:
    file_stat = os.fstat(log.fileno())
    if file_stat.st_size < offset:
        return _LogObservation(conflicting=True, log_device=file_stat.st_dev, log_inode=file_stat.st_ino, log_start_offset=offset)
    log.seek(offset)
    raw = log.read(_LOG_LIMIT + 1)
    truncated = len(raw) > _LOG_LIMIT
    text = raw[:_LOG_LIMIT].decode("utf-8", errors="replace")
    decoder = json.JSONDecoder()
    handlers: set[float] = set()
    returns: set[float] = set()
    completed: set[bool] = set()
    forced: set[bool] = set()
    origins: set[float] = set()
    matched = 0
    for marker in re.finditer(r"shutdown (?:progress|completed):\s*", text):
        if matched >= 64:
            truncated = True
            break
        try:
            value, _end = decoder.raw_decode(text, marker.end())
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict) or not _same_identity(value.get("process_identity"), identity):
            continue
        origin = _number(value.get("started_at_monotonic"))
        timeline = value.get("timeline")
        if origin is None or origin < started or not isinstance(timeline, list):
            continue
        matched += 1
        origins.add(origin)
        for span in timeline[:256]:
            if not isinstance(span, dict):
                continue
            elapsed = _number(span.get("started_seconds"))
            if elapsed is None or elapsed < 0:
                continue
            if span.get("name") == "signal_received" and elapsed == 0:
                handlers.add(origin)
            elif span.get("name") == "return_ready":
                returns.add(origin + elapsed)
        report = value.get("report")
        if isinstance(report, dict):
            if isinstance(report.get("completed"), bool):
                completed.add(report["completed"])
            if isinstance(report.get("forced"), bool):
                forced.add(report["forced"])
    return _LogObservation(
        next(iter(handlers)) if len(handlers) == 1 else None,
        next(iter(returns)) if len(returns) == 1 else None,
        next(iter(completed)) if len(completed) == 1 else None,
        next(iter(forced)) if len(forced) == 1 else None,
        matched, min(len(raw), _LOG_LIMIT), truncated,
        any(len(values) > 1 for values in (handlers, returns, completed, forced, origins)),
        file_stat.st_dev, file_stat.st_ino, offset,
    )


def _same_identity(value: object, identity: LinuxProcessIdentity) -> bool:
    return isinstance(value, Mapping) and value.get("pid") == identity.pid and (
        value.get("birth_ticks") == identity.birth_ticks and value.get("boot_id") == identity.boot_id
        and type(value.get("pid")) is int and type(value.get("birth_ticks")) is int
    )


def _difference(end: float | None, start: float | None) -> float | None:
    return None if end is None or start is None else end - start


def _outcome(
    options: ShutdownObservationOptions, identity: LinuxProcessIdentity,
    times: _Times, observed: _LogObservation, *, cancelled: bool,
) -> ToolOutcome:
    elapsed = _difference(times.exit_observed, times.signal_send_started)
    within = None if elapsed is None else elapsed <= _SHUTDOWN_LIMIT_SECONDS
    ordered = observed.handler_started is not None and observed.return_ready is not None and (
        times.observation_started <= observed.handler_started <= observed.return_ready
        and (not options.send_sigterm or (
            times.signal_send_started is not None and times.signal_send_started <= observed.handler_started
        ))
        and (times.exit_observed is None or observed.return_ready <= times.exit_observed)
    )
    time_conflict = observed.handler_started is not None and observed.return_ready is not None and not ordered
    findings: list[str] = []
    if observed.conflicting or (observed.matched_records and not ordered):
        findings.append("shutdown_observation_timeline_conflicting_or_incomplete")
    if observed.truncated:
        findings.append("shutdown_observation_log_limit")
    if not observed.matched_records:
        findings.append("shutdown_observation_process_events_missing")
    if not options.send_sigterm:
        findings.append("shutdown_observation_external_signal_time_unavailable")
    if within is False:
        findings.append("shutdown_observation_exit_over_30_seconds")
    if observed.resource_completed is False or observed.resource_forced is True:
        findings.append("shutdown_observation_resource_cleanup_not_completed")
    status: CheckStatus = "passed"
    if cancelled:
        status = "cancelled"
    elif times.exit_observed is None:
        status = "timed_out"
        findings.append("shutdown_observation_exit_not_observed")
    elif observed.conflicting or time_conflict or within is False or observed.resource_completed is False or observed.resource_forced is True:
        status = "failed"
    elif findings or not ordered or observed.resource_completed is None or observed.resource_forced is None:
        status = "degraded"
    summary: dict[str, object] = {
        "read_only": not options.send_sigterm,
        "process_identity": {"pid": identity.pid, "birth_ticks": identity.birth_ticks, "boot_id": identity.boot_id},
        "sigterm_sent": times.signal_send_finished is not None,
        "observation_seconds": options.observation_seconds,
        "shutdown_limit_seconds": _SHUTDOWN_LIMIT_SECONDS,
        "process_exit_observed": times.exit_observed is not None,
        "within_shutdown_limit": within,
        "timeline_complete": bool(ordered and options.send_sigterm and not observed.conflicting and not observed.truncated),
        "runtime_shutdown_acceptance": False,
        "target_exit_code": None,
        "resource_report_completed": observed.resource_completed,
        "resource_report_forced": observed.resource_forced,
        "timeline": {
            "observation_started_monotonic": times.observation_started,
            "signal_send_started_monotonic": times.signal_send_started,
            "signal_send_finished_monotonic": times.signal_send_finished,
            "python_handler_started_monotonic": observed.handler_started,
            "business_return_ready_monotonic": observed.return_ready,
            "pidfd_wait_started_monotonic": times.pidfd_wait_started,
            "pidfd_wait_finished_monotonic": times.pidfd_wait_finished,
            "pidfd_exit_observed_monotonic": times.exit_observed,
        },
        "durations": {
            "signal_to_handler_seconds": _difference(observed.handler_started, times.signal_send_started),
            "handler_to_business_return_seconds": _difference(observed.return_ready, observed.handler_started),
            "business_return_to_exit_observed_seconds": _difference(times.exit_observed, observed.return_ready),
            "signal_to_exit_observed_seconds": elapsed,
        },
        "log_observation": {"matched_records": observed.matched_records, "bytes_read": observed.bytes_read,
                            "truncated": observed.truncated, "device": observed.log_device,
                            "inode": observed.log_inode, "start_offset": observed.log_start_offset},
        "clock_scope": "same_boot_system_monotonic; pidfd_readiness_is_observed_exit_not_exact_kernel_exit_time",
    }
    return ToolOutcome(130 if cancelled else 124 if status == "timed_out" else 1 if status == "failed" else 0, {
        "schema_version": "shutdown-observation", "status": status, "summary": summary, "findings": findings,
    })
