#!/usr/bin/env python3
"""Run an isolated BaoStock serial rate-limit experiment.

This diagnostic never opens the repository archive and never calls the history
sync/checkpoint path. The ``--serial-only`` mode uses one short-lived login and
never creates worker processes.
"""

from __future__ import annotations

import argparse
import io
import math
import socket
import sys
import time
from collections.abc import Iterable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from multiprocessing import get_context
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from .reporting import emit_report

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trader.download.infra.baostock_session import (  # noqa: E402
    BaoStockSessionSdkPort,
    RateLimitedBaoStockSdk,
    load_baostock_sdk,
    login_baostock,
    logout_baostock,
)

_DEFAULT_CODES = (
    "600519",
    "000001",
    "300750",
    "688981",
    "601318",
    "600036",
    "000333",
    "002594",
    "601012",
    "600900",
)
_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST"
Mode = Literal["serial", "parallel"]


@dataclass(frozen=True)
class CodeResult:
    ok: bool
    raw_rows: int
    qfq_rows: int
    consistency_ok: bool
    network_errors: int
    timeouts: int
    retries: int
    error: str | None
    elapsed_ms: float


@dataclass(frozen=True)
class WorkerCommand:
    codes: tuple[str, ...]
    days: int
    timeout_seconds: float
    interval_seconds: float


@dataclass(frozen=True)
class SerialRatePlan:
    intervals: tuple[float, ...]
    rounds: int = 1

    def __post_init__(self) -> None:
        if not 1 <= self.rounds <= 3 or not 1 <= len(self.intervals) <= 3:
            raise ValueError("serial rate plan accepts one to three rounds and intervals")
        if len(set(self.intervals)) != len(self.intervals) or any(
            not math.isfinite(value) or value < 1 for value in self.intervals
        ):
            raise ValueError("serial rate intervals must be unique finite values of at least one second")
        object.__setattr__(self, "intervals", tuple(self.intervals))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes", nargs="+", default=_DEFAULT_CODES)
    parser.add_argument("--days", type=int, default=61)
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--interval-seconds", type=float, default=2.0)
    parser.add_argument("--intervals", nargs="+", type=float, help="serial-only candidate intervals in seconds")
    parser.add_argument("--sizes", nargs="+", type=int, default=(10, 50, 100))
    parser.add_argument("--discover", action="store_true", help="discover a bounded stock universe from BaoStock")
    parser.add_argument("--serial-only", action="store_true", help="run only one serial BaoStock session")
    parser.add_argument("--rounds", type=int, default=1, help="serial-only repetitions within 1..3")
    return parser


def _validate(args: argparse.Namespace) -> tuple[str, ...]:
    codes = tuple(dict.fromkeys(args.codes))
    if not args.discover and (not codes or len(codes) < max(args.sizes)):
        raise ValueError("--codes must contain at least the largest requested sample size")
    if any(len(code) != 6 or not code.isdigit() for code in codes):
        raise ValueError("--codes must contain six-digit A-share codes")
    if any(size < 1 or size > 100 for size in args.sizes):
        raise ValueError("--sizes must be within 1..100")
    if (
        args.days < 1
        or not math.isfinite(args.timeout_seconds)
        or args.timeout_seconds <= 0
        or args.interval_seconds < 2.0
    ):
        raise ValueError("days/timeout must be positive and interval must be at least 2 seconds")
    if (
        args.serial_only
        and args.intervals is not None
        and any(not math.isfinite(interval) or interval < 1.0 for interval in args.intervals)
    ):
        raise ValueError("serial-only experiment intervals must be at least 1 second")
    if not 1 <= args.rounds <= 3 or (not args.serial_only and args.rounds != 1):
        raise ValueError("rounds must be within 1..3 and repeated experiments must be serial-only")
    if args.intervals is not None and not args.serial_only:
        raise ValueError("interval comparisons require serial-only mode")
    return codes


def _discover_codes(limit: int) -> tuple[str, ...]:
    sdk = None
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            sdk = _open()
            rows = _rows(sdk.query_stock_basic())
        values = tuple(
            sorted(
                row["code"].split(".")[-1]
                for row in rows
                if row.get("type") == "1" and row.get("status") == "1" and len(row.get("code", "").split(".")[-1]) == 6
            )
        )
        if len(values) < limit:
            raise RuntimeError("supplier_universe_too_small")
        groups = tuple(
            tuple(
                code
                for code in values
                if code.startswith(prefix) and (prefix != ("0", "6") or not code.startswith("688"))
            )
            for prefix in (("0", "6"), ("3",), ("688",))
        )
        selected: list[str] = []
        for index in range(limit):
            for group in groups:
                if index < len(group) and group[index] not in selected:
                    selected.append(group[index])
        if len(selected) < limit:
            raise RuntimeError("supplier_universe_too_small")
        return tuple(selected[:limit])
    finally:
        if sdk is not None:
            _close(sdk)


def _source_code(code: str) -> str:
    return f"sh.{code}" if code.startswith(("5", "6", "9")) else f"sz.{code}"


def _rows(result: object) -> list[dict[str, str]]:
    error_code = getattr(result, "error_code", "0")
    if str(error_code) != "0":
        raise RuntimeError(f"network_error_{str(error_code)[:32]}")
    raw_fields = getattr(result, "fields", ())
    fields = raw_fields.split(",") if isinstance(raw_fields, str) else tuple(raw_fields)
    values: list[dict[str, str]] = []
    while bool(result.next()):  # type: ignore[attr-defined]
        row = result.get_row_data()  # type: ignore[attr-defined]
        values.append(dict(zip(fields, row, strict=False)))
    return values


def _query_one(sdk: RateLimitedBaoStockSdk, code: str, start: date, end: date, adjustflag: str) -> list[dict[str, str]]:
    return _rows(
        sdk.query_history_k_data_plus(
            _source_code(code),
            _FIELDS,
            start.isoformat(),
            end.isoformat(),
            frequency="d",
            adjustflag=adjustflag,
        )
    )


def _one(sdk: RateLimitedBaoStockSdk, code: str, *, days: int) -> CodeResult:
    started = time.monotonic()
    end = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    start = end - timedelta(days=days)
    try:
        raw = _query_one(sdk, code, start, end, "3")
        qfq = _query_one(sdk, code, start, end, "2")
        raw_dates = tuple(row.get("date", "") for row in raw)
        qfq_dates = tuple(row.get("date", "") for row in qfq)
        consistent = (
            bool(raw)
            and bool(qfq)
            and raw_dates == qfq_dates
            and raw_dates == tuple(sorted(set(raw_dates)))
            and all(
                start.isoformat() <= value <= end.isoformat() and date.fromisoformat(value).isoformat() == value
                for value in raw_dates
            )
        )
        return CodeResult(
            bool(raw) and bool(qfq) and consistent,
            len(raw),
            len(qfq),
            consistent,
            0,
            0,
            0,
            None if consistent else "raw_qfq_inconsistent",
            round((time.monotonic() - started) * 1000.0, 1),
        )
    except TimeoutError:
        return CodeResult(
            False, 0, 0, False, 0, 1, 0, "supplier_timeout", round((time.monotonic() - started) * 1000.0, 1)
        )
    except (OSError, RuntimeError, TypeError, ValueError, UnboundLocalError) as exc:
        error = _error_code(exc)
        return CodeResult(
            False,
            0,
            0,
            False,
            int(error.startswith("network_error")),
            0,
            0,
            error,
            round((time.monotonic() - started) * 1000.0, 1),
        )


def _open() -> BaoStockSessionSdkPort:
    sdk = load_baostock_sdk()
    login_baostock(sdk)
    return sdk


def _close(sdk: BaoStockSessionSdkPort) -> None:
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        logout_baostock(sdk)


def _serial(command: WorkerCommand) -> tuple[CodeResult, ...]:
    sdk = None
    previous_timeout = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(command.timeout_seconds)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            sdk = _open()
            limited = RateLimitedBaoStockSdk(sdk, interval_seconds=command.interval_seconds)
            results: list[CodeResult] = []
            for code in command.codes:
                result = _one(limited, code, days=command.days)
                results.append(result)
                if not result.ok:
                    results.extend(
                        CodeResult(False, 0, 0, False, 0, 0, 0, "supplier_batch_stopped", 0.0)
                        for _ in range(len(command.codes) - len(results))
                    )
                    break
            return tuple(results)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        error = _error_code(exc)
        return tuple(
            CodeResult(
                False,
                0,
                0,
                False,
                int(index == 0 and error.startswith("network_error")),
                int(index == 0 and error == "supplier_timeout"),
                0,
                error,
                0.0,
            )
            for index, _ in enumerate(command.codes)
        )
    finally:
        if sdk is not None:
            _close(sdk)
        socket.setdefaulttimeout(previous_timeout)


def _parallel_worker(connection: Connection, command: WorkerCommand) -> None:
    try:
        connection.send(_serial(command))
    except (EOFError, OSError):
        pass
    finally:
        connection.close()


def _parallel(command: WorkerCommand, timeout_seconds: float) -> tuple[CodeResult, ...]:
    context = get_context("spawn")
    groups = (command.codes[::2], command.codes[1::2])
    pipes: list[tuple[Connection, Connection]] = []
    processes = []
    started = time.monotonic()
    try:
        for group in groups:
            parent, child = context.Pipe(duplex=False)
            process = context.Process(
                target=_parallel_worker,
                args=(child, WorkerCommand(group, command.days, command.timeout_seconds, command.interval_seconds)),
                daemon=True,
            )
            process.start()
            child.close()
            pipes.append((parent, child))
            processes.append(process)
        results: list[CodeResult] = []
        for parent, _child in pipes:
            remaining = max(0.1, timeout_seconds - (time.monotonic() - started))
            if not parent.poll(remaining):
                results.extend(
                    CodeResult(False, 0, 0, False, 0, 1, 0, "supplier_call_timeout", 0.0)
                    for _ in range(len(command.codes) // 2 + (len(command.codes) % 2))
                )
            else:
                received = parent.recv()
                results.extend(received if isinstance(received, tuple) else ())
        return tuple(results)
    except (EOFError, OSError, RuntimeError):
        return tuple(CodeResult(False, 0, 0, False, 0, 1, 0, "probe_process_failed", 0.0) for _ in command.codes)
    finally:
        for parent, _child in pipes:
            parent.close()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=1.0)


def _error_code(exc: BaseException) -> str:
    message = str(exc).lower()
    if "timeout" in message or isinstance(exc, TimeoutError):
        return "supplier_timeout"
    if message.startswith("network_error_"):
        return "network_error"
    if "blacklisted" in message:
        return "supplier_blacklisted"
    if "transport" in message or "login" in message or isinstance(exc, UnboundLocalError):
        return "supplier_login_transport_failed"
    return type(exc).__name__.lower()[:48]


def _summary(mode: Mode, size: int, results: Iterable[CodeResult], elapsed_ms: float) -> dict[str, object]:
    values = tuple(results)
    error_categories = sorted({item.error for item in values if item.error is not None})
    if len(values) != size:
        error_categories.append("sample_count_mismatch")
    return {
        "mode": mode,
        "sample_size": size,
        "success_count": sum(item.ok for item in values),
        "failure_count": size - sum(item.ok for item in values) if len(values) == size else size,
        "success_rate": round(sum(item.ok for item in values) / size, 4) if size else 0.0,
        "network_receive_errors": sum(item.network_errors for item in values),
        "timeouts": sum(item.timeouts for item in values),
        "retries": sum(item.retries for item in values),
        "raw_rows": sum(item.raw_rows for item in values),
        "qfq_rows": sum(item.qfq_rows for item in values),
        "raw_qfq_inconsistencies": sum(
            item.raw_rows > 0 and item.qfq_rows > 0 and not item.consistency_ok for item in values
        ),
        "error_categories": error_categories,
        "elapsed_ms": round(elapsed_ms, 1),
        "average_code_ms": round(sum(item.elapsed_ms for item in values) / size, 1) if size else 0.0,
        "successful_codes_per_minute": round(sum(item.ok for item in values) * 60000 / elapsed_ms, 2)
        if elapsed_ms
        else 0.0,
    }


def run(
    codes: tuple[str, ...], sizes: tuple[int, ...], *, days: int, timeout_seconds: float, interval_seconds: float
) -> dict[str, object]:
    experiments: list[dict[str, object]] = []
    for size in sizes:
        sample = codes[:size]
        command = WorkerCommand(sample, days, timeout_seconds, interval_seconds)
        for mode in ("serial", "parallel"):
            started = time.monotonic()
            results = (
                _serial(command)
                if mode == "serial"
                else _parallel(command, timeout_seconds * 2 + size * interval_seconds)
            )
            experiments.append(_summary(mode, size, results, (time.monotonic() - started) * 1000.0))
    passed = all(item["failure_count"] == 0 and item["raw_qfq_inconsistencies"] == 0 for item in experiments)
    return {
        "schema_version": "baostock-concurrency-experiment",
        "status": "passed" if passed else "failed",
        "production_eligible": False,
        "experiments": experiments,
    }


def run_serial_intervals(
    codes: tuple[str, ...],
    sizes: tuple[int, ...],
    *,
    days: int,
    timeout_seconds: float,
    plan: SerialRatePlan,
) -> dict[str, object]:
    """Compare candidate intervals with at most one active SDK session."""

    experiments: list[dict[str, object]] = []
    for round_index in range(1, plan.rounds + 1):
        for interval_seconds in plan.intervals:
            for size in sizes:
                command = WorkerCommand(codes[:size], days, timeout_seconds, interval_seconds)
                started = time.monotonic()
                results = _serial(command)
                summary = _summary("serial", size, results, (time.monotonic() - started) * 1000.0)
                summary["interval_seconds"] = interval_seconds
                summary["round"] = round_index
                experiments.append(summary)
                if summary["failure_count"] != 0 or any(item.error is not None for item in results):
                    return _serial_report(experiments, days)
    return _serial_report(experiments, days)


def _serial_report(experiments: list[dict[str, object]], days: int) -> dict[str, object]:
    return {
        "schema_version": "baostock-serial-rate-experiment",
        "status": "passed"
        if all(item["failure_count"] == 0 and item["raw_qfq_inconsistencies"] == 0 for item in experiments)
        else "failed",
        "production_eligible": False,
        "parallel_sessions": 0,
        "history_calendar_days": days,
        "rate_semantics": "query_start_to_start",
        "retry_policy": "no_retries",
        "experiments": experiments,
    }


def main() -> int:
    args = _parser().parse_args()
    try:
        codes = _validate(args)
        if args.discover:
            codes = _discover_codes(max(args.sizes))
        if args.serial_only:
            report = run_serial_intervals(
                codes,
                tuple(args.sizes),
                days=args.days,
                timeout_seconds=args.timeout_seconds,
                plan=SerialRatePlan(tuple(args.intervals or (args.interval_seconds,)), args.rounds),
            )
        else:
            report = run(
                codes,
                tuple(args.sizes),
                days=args.days,
                timeout_seconds=args.timeout_seconds,
                interval_seconds=args.interval_seconds,
            )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        report = {
            "schema_version": "baostock-concurrency-experiment",
            "status": "failed",
            "production_eligible": False,
            "error": _error_code(exc),
        }
    emit_report(report)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
