"""Bounded BaoStock worker for filling provable gaps in converted daily history."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from multiprocessing import get_context
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Literal, cast

from trader.domain.research.baostock_active_archive import (
    BaoStockArchiveFieldFamily,
    BaoStockArchiveRecordKey,
)
from trader.infra.research.baostock_active_archive import BaoStockIncrementRecord
from trader.infra.research.baostock_history_messages import SupplierCallActivity
from trader.infra.research.baostock_history_runtime import (
    _RateLimitedBaoStockSdk,
    _failure_code,
    _load_sdk,
    _login,
    _logout,
)
from trader.infra.research.baostock_increment_runtime import _BaoStockIncrementSource

BaoStockGapFamily = Literal["daily_raw", "daily_qfq", "is_st"]
GapProgress = Callable[[int, int, str], None]
CancelRequested = Callable[[], bool]
_SECURITY_CODE_LENGTH = 6


class BaoStockGapSupplierError(RuntimeError):
    """The bounded supplier worker could not prove all requested gap results."""


@dataclass(frozen=True, order=True)
class BaoStockGapRequest:
    code: str
    family: BaoStockGapFamily
    trade_dates: tuple[date, ...]

    def __post_init__(self) -> None:
        dates = tuple(sorted(set(self.trade_dates)))
        if (
            len(self.code) != _SECURITY_CODE_LENGTH
            or not self.code.isdigit()
            or self.family not in {"daily_raw", "daily_qfq", "is_st"}
            or not dates
        ):
            raise ValueError("BaoStock gap request is invalid")
        object.__setattr__(self, "trade_dates", dates)


@dataclass(frozen=True, order=True)
class BaoStockGapRecord:
    code: str
    trade_date: date
    family: BaoStockGapFamily
    payload_json: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        try:
            validated = BaoStockIncrementRecord(
                BaoStockArchiveRecordKey(self.code, self.trade_date, self.family),
                self.payload_json,
            )
        except ValueError as exc:
            raise ValueError("BaoStock gap record identity is invalid") from exc
        object.__setattr__(self, "content_hash", validated.content_hash)


@dataclass(frozen=True, order=True)
class BaoStockGapUnavailable:
    code: str
    trade_date: date
    family: BaoStockGapFamily
    reason: str

    def __post_init__(self) -> None:
        if (
            len(self.code) != _SECURITY_CODE_LENGTH
            or not self.code.isdigit()
            or self.family not in {"daily_raw", "daily_qfq", "is_st"}
            or not self.reason
        ):
            raise ValueError("BaoStock gap unavailable reason is missing")


@dataclass(frozen=True)
class BaoStockGapResult:
    records: tuple[BaoStockGapRecord, ...]
    unavailable: tuple[BaoStockGapUnavailable, ...]

    def __post_init__(self) -> None:
        records = tuple(sorted(self.records))
        unavailable = tuple(sorted(self.unavailable))
        record_keys = {(item.code, item.trade_date, item.family) for item in records}
        unavailable_keys = {(item.code, item.trade_date, item.family) for item in unavailable}
        if (
            len(record_keys) != len(records)
            or len(unavailable_keys) != len(unavailable)
            or record_keys & unavailable_keys
        ):
            raise ValueError("BaoStock gap result contains duplicate identities")
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "unavailable", unavailable)


@dataclass(frozen=True)
class _WorkerReady:
    failure_reason: str = ""


@dataclass(frozen=True)
class _WorkerResponse:
    request: BaoStockGapRequest
    records: tuple[BaoStockGapRecord, ...] = ()
    unavailable: tuple[BaoStockGapUnavailable, ...] = ()
    failure_reason: str = ""


@dataclass(frozen=True)
class _WorkerDone:
    pass


@dataclass(frozen=True)
class _MonitorOptions:
    requests: tuple[BaoStockGapRequest, ...]
    timeout_seconds: float
    cancel_requested: CancelRequested
    progress: GapProgress


class _WorkerMonitor:
    def __init__(
        self,
        parent: Connection,
        process: BaseProcess,
        options: _MonitorOptions,
    ) -> None:
        self._parent = parent
        self._process = process
        self._requests = options.requests
        self._total = len(options.requests)
        self._timeout_seconds = options.timeout_seconds
        self._cancel_requested = options.cancel_requested
        self._progress = options.progress
        self._deadline = time.monotonic() + options.timeout_seconds
        self._records: list[BaoStockGapRecord] = []
        self._unavailable: list[BaoStockGapUnavailable] = []
        self._completed = 0

    def run(self) -> BaoStockGapResult:
        try:
            while True:
                message = self._next_message()
                if isinstance(message, _WorkerDone):
                    if self._completed != self._total:
                        raise BaoStockGapSupplierError("supplier worker returned an incomplete result")
                    self._process.join(timeout=1.0)
                    return BaoStockGapResult(tuple(self._records), tuple(self._unavailable))
                self._accept(message)
        except (EOFError, OSError) as exc:
            raise BaoStockGapSupplierError("supplier worker connection failed") from exc
        finally:
            self._parent.close()
            _terminate(self._process)

    def _next_message(self) -> object:
        while True:
            if self._cancel_requested():
                raise BaoStockGapSupplierError("gap download cancelled")
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise BaoStockGapSupplierError("supplier call timed out")
            if self._parent.poll(min(0.25, remaining)):
                return self._parent.recv()
            if not self._process.is_alive():
                raise BaoStockGapSupplierError("supplier worker exited unexpectedly")

    def _accept(self, message: object) -> None:
        if isinstance(message, SupplierCallActivity):
            self._refresh_deadline()
            return
        if isinstance(message, _WorkerReady):
            self._raise_failure(message.failure_reason)
            self._refresh_deadline()
            return
        if isinstance(message, _WorkerResponse):
            self._raise_failure(message.failure_reason)
            if self._completed >= self._total or message.request != self._requests[self._completed]:
                raise BaoStockGapSupplierError("supplier worker response order is invalid")
            expected = {
                (message.request.code, trade_date, message.request.family) for trade_date in message.request.trade_dates
            }
            returned = {(item.code, item.trade_date, item.family) for item in message.records}
            returned.update((item.code, item.trade_date, item.family) for item in message.unavailable)
            if returned != expected:
                raise BaoStockGapSupplierError("supplier worker response is incomplete")
            self._records.extend(message.records)
            self._unavailable.extend(message.unavailable)
            self._completed += 1
            self._progress(self._completed, self._total, f"{message.request.code}:{message.request.family}")
            self._refresh_deadline()
            return
        raise BaoStockGapSupplierError("supplier worker protocol is invalid")

    def _refresh_deadline(self) -> None:
        self._deadline = time.monotonic() + self._timeout_seconds

    @staticmethod
    def _raise_failure(reason: str) -> None:
        if reason:
            raise BaoStockGapSupplierError(reason)


def fetch_baostock_gaps(
    requests: tuple[BaoStockGapRequest, ...],
    *,
    retries: int = 2,
    call_timeout_seconds: float = 60.0,
    cancel_requested: CancelRequested = lambda: False,
    progress: GapProgress = lambda _completed, _total, _current: None,
) -> BaoStockGapResult:
    """Fetch all requests in one rate-limited SDK process with bounded calls."""

    ordered = tuple(sorted(requests))
    if not ordered:
        return BaoStockGapResult((), ())
    if len(set(ordered)) != len(ordered) or retries not in range(3) or call_timeout_seconds <= 0:
        raise ValueError("BaoStock gap worker options are invalid")
    context = get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_worker_main, args=(child, ordered, retries), daemon=True)
    process.start()
    child.close()
    return _WorkerMonitor(
        parent,
        process,
        _MonitorOptions(ordered, call_timeout_seconds, cancel_requested, progress),
    ).run()


def _worker_main(connection: Connection, requests: tuple[BaoStockGapRequest, ...], retries: int) -> None:
    sdk = None
    try:
        sdk = _load_sdk()
        _login(sdk)
        limited = _RateLimitedBaoStockSdk(
            sdk,
            activity=lambda state: connection.send(SupplierCallActivity(state)),
        )
        source = _BaoStockIncrementSource(limited)
        connection.send(_WorkerReady())
        for request in requests:
            response = _fetch_one(source, request, retries)
            connection.send(response)
            if response.failure_reason:
                break
        else:
            connection.send(_WorkerDone())
    except Exception as exc:
        connection.send(_WorkerReady(_failure_code(exc)))
    finally:
        if sdk is not None:
            _logout(sdk)
        connection.close()


def _fetch_one(
    source: _BaoStockIncrementSource,
    request: BaoStockGapRequest,
    retries: int,
) -> _WorkerResponse:
    for attempt in range(retries + 1):
        try:
            result = source.fetch_daily(
                request.code,
                cast(BaoStockArchiveFieldFamily, request.family),
                request.trade_dates,
            )
            records = tuple(
                BaoStockGapRecord(
                    item.key.code,
                    item.key.trade_date,
                    item.key.family,
                    item.payload_json,
                )
                for item in result.records
                if item.key.family == request.family
            )
            unavailable = tuple(
                BaoStockGapUnavailable(
                    item.code,
                    item.trade_date,
                    cast(BaoStockGapFamily, item.family),
                    result.unavailable_reason or "supplier_adjustment_unavailable",
                )
                for item in result.unavailable_keys
            )
            return _WorkerResponse(request, records, unavailable)
        except Exception as exc:
            reason = _failure_code(exc)
            if reason == "supplier_query_failed_blacklisted" or attempt == retries:
                return _WorkerResponse(request, failure_reason=reason)
    raise AssertionError("unreachable BaoStock gap retry state")


def _terminate(process: BaseProcess) -> None:
    if process.is_alive():
        process.terminate()
    process.join(timeout=10.0)
    if process.is_alive() and hasattr(process, "kill"):
        process.kill()
        process.join(timeout=1.0)


__all__ = [
    "BaoStockGapFamily",
    "BaoStockGapRecord",
    "BaoStockGapRequest",
    "BaoStockGapResult",
    "BaoStockGapSupplierError",
    "BaoStockGapUnavailable",
    "fetch_baostock_gaps",
]
