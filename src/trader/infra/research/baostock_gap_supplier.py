"""Bounded BaoStock worker for filling provable gaps in converted daily history."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from multiprocessing import get_context
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Literal

from trader.domain.research.baostock_daily import BaoStockAdjustment
from trader.infra.research.baostock_gateway import (
    _DAILY_FACT_FIELDS,
    _DAILY_FIELDS,
    BaoStockSdkPort,
    _daily_side,
    _is_st,
    _result_rows,
    qfq_source_windows,
)
from trader.infra.research.baostock_session import (
    BaoStockSessionSdkPort,
    RateLimitedBaoStockSdk,
    load_baostock_sdk,
    login_baostock,
    logout_baostock,
)

BaoStockGapFamily = Literal["daily_raw", "daily_qfq", "is_st"]
GapProgress = Callable[[int, int, str], None]
CancelRequested = Callable[[], bool]
_SECURITY_CODE_LENGTH = 6


class BaoStockGapSupplierError(RuntimeError):
    """The bounded supplier worker could not prove all requested gap results."""


@dataclass(frozen=True)
class _SupplierCallActivity:
    state: Literal["started", "completed"]


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
            payload = json.loads(self.payload_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("BaoStock gap record identity is invalid") from exc
        if not _valid_gap_payload(self.code, self.trade_date, self.family, payload):
            raise ValueError("BaoStock gap record identity is invalid")
        canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        if canonical != self.payload_json:
            raise ValueError("BaoStock gap record identity is invalid")
        object.__setattr__(self, "content_hash", hashlib.sha256(canonical.encode()).hexdigest())


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
        if isinstance(message, _SupplierCallActivity):
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
    sdk: BaoStockSessionSdkPort | None = None
    try:
        sdk = load_baostock_sdk()
        login_baostock(sdk)
        limited = RateLimitedBaoStockSdk(
            sdk,
            activity=lambda state: connection.send(_SupplierCallActivity(state)),
        )
        connection.send(_WorkerReady())
        for request in requests:
            response = _fetch_one(limited, request, retries)
            connection.send(response)
            if response.failure_reason:
                break
        else:
            connection.send(_WorkerDone())
    except Exception as exc:
        connection.send(_WorkerReady(_failure_code(exc)))
    finally:
        if sdk is not None:
            logout_baostock(sdk)
        connection.close()


def _fetch_one(
    source: BaoStockSdkPort,
    request: BaoStockGapRequest,
    retries: int,
) -> _WorkerResponse:
    for attempt in range(retries + 1):
        try:
            records, unavailable = _fetch_request(source, request)
            return _WorkerResponse(request, records, unavailable)
        except Exception as exc:
            reason = _failure_code(exc)
            if reason == "supplier_query_failed_blacklisted" or attempt == retries:
                return _WorkerResponse(request, failure_reason=reason)
    raise AssertionError("unreachable BaoStock gap retry state")


def _fetch_request(
    source: BaoStockSdkPort,
    request: BaoStockGapRequest,
) -> tuple[tuple[BaoStockGapRecord, ...], tuple[BaoStockGapUnavailable, ...]]:
    source_code = ("sh." if request.code.startswith("6") else "sz.") + request.code
    expected = frozenset(request.trade_dates)
    fields = _DAILY_FACT_FIELDS if request.family == "is_st" else _DAILY_FIELDS
    adjustflag = "3" if request.family in {"daily_raw", "is_st"} else "2"
    windows = (
        qfq_source_windows(source_code, request.trade_dates)
        if request.family == "daily_qfq"
        else ((source_code, request.trade_dates),)
    )
    records: list[BaoStockGapRecord] = []
    unavailable: list[BaoStockGapUnavailable] = []
    observed: set[date] = set()
    for query_code, query_dates in windows:
        rows = _result_rows(
            source.query_history_k_data_plus(
                query_code,
                fields,
                query_dates[0].isoformat(),
                query_dates[-1].isoformat(),
                frequency="d",
                adjustflag=adjustflag,
            ),
            f"{request.family}_query_failed",
        )
        for row in rows:
            day = date.fromisoformat(row.get("date", ""))
            if day not in expected:
                continue
            if day in observed or row.get("code") != query_code or row.get("tradestatus") not in {"0", "1"}:
                raise ValueError("BaoStock gap response identity is invalid")
            observed.add(day)
            if request.family == "daily_qfq" and row.get("adjustflag") == "3":
                unavailable.append(
                    BaoStockGapUnavailable(
                        request.code,
                        day,
                        request.family,
                        "supplier_adjustment_unavailable",
                    )
                )
                continue
            if request.family != "is_st" and row.get("adjustflag") != adjustflag:
                raise ValueError("BaoStock gap response adjustment is invalid")
            payload = (
                {"code": request.code, "trade_date": day.isoformat(), "is_st": _is_st(row.get("isST"))}
                if request.family == "is_st"
                else _side_payload(request.code, day, request.family, row)
            )
            records.append(
                BaoStockGapRecord(
                    request.code,
                    day,
                    request.family,
                    json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                )
            )
    if observed != expected:
        raise RuntimeError("supplier_response_incomplete")
    return tuple(records), tuple(unavailable)


def _side_payload(
    code: str,
    day: date,
    family: BaoStockGapFamily,
    row: dict[str, str],
) -> dict[str, object]:
    adjustment: BaoStockAdjustment = "unadjusted" if family == "daily_raw" else "qfq"
    side = _daily_side(code, day, adjustment, row)
    return {
        "code": code,
        "trade_date": day.isoformat(),
        "adjustment": side.adjustment,
        "open_price": side.open_price,
        "high_price": side.high_price,
        "low_price": side.low_price,
        "close_price": side.close_price,
        "volume": side.volume,
        "amount": side.amount,
        "preclose": side.preclose,
        "pct_change": side.pct_change,
        "turnover": side.turnover,
        "trading_status": side.trading_status,
    }


def _valid_gap_payload(
    code: str,
    trade_date: date,
    family: BaoStockGapFamily,
    payload: object,
) -> bool:
    if (
        not isinstance(payload, dict)
        or payload.get("code") != code
        or payload.get("trade_date") != trade_date.isoformat()
    ):
        return False
    if family == "is_st":
        return set(payload) == {"code", "trade_date", "is_st"} and isinstance(payload.get("is_st"), bool)
    numeric = {
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "volume",
        "amount",
        "preclose",
        "pct_change",
        "turnover",
    }
    expected = {"code", "trade_date", "adjustment", "trading_status", *numeric}
    adjustment = "unadjusted" if family == "daily_raw" else "qfq"
    return (
        set(payload) == expected
        and payload.get("adjustment") == adjustment
        and payload.get("trading_status") in {"trading", "suspended"}
        and all(
            value is None or isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            for key, value in payload.items()
            if key in numeric
        )
        and (family == "daily_raw" or all(payload.get(key) is None for key in ("preclose", "pct_change", "turnover")))
    )


def _failure_code(exc: BaseException) -> str:
    value = str(exc).strip()
    return value if value and len(value) <= 64 and value.replace("_", "").isalnum() else "supplier_failed"


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
