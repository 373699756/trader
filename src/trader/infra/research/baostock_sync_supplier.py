"""Single-process BaoStock session for zero-argument history synchronization."""

from __future__ import annotations

import importlib.metadata
import os
import platform
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from multiprocessing import get_context
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Literal, Protocol, cast

from trader.application.research.history_sync import HistorySupplierContext
from trader.domain.research.baostock_daily import (
    BaoStockCalendar,
    BaoStockCodeDownload,
    BaoStockDailySpec,
    BaoStockSecurity,
)
from trader.infra.research.baostock_gateway import BaoStockRowGateway, BaoStockRowResult, BaoStockSdkPort


class _SessionSdk(BaoStockSdkPort, Protocol):
    def login(self) -> BaoStockRowResult: ...

    def logout(self) -> BaoStockRowResult: ...


@dataclass(frozen=True)
class _LoadContext:
    as_of: date
    sessions: int


@dataclass(frozen=True)
class _FetchCode:
    security: BaoStockSecurity
    dates: tuple[date, ...]


@dataclass(frozen=True)
class _Stop:
    pass


@dataclass(frozen=True)
class _Activity:
    state: Literal["started", "completed"]


@dataclass(frozen=True)
class _Ready:
    failure_reason: str | None = None


@dataclass(frozen=True)
class _Response:
    context: HistorySupplierContext | None = None
    download: BaoStockCodeDownload | None = None
    failure_reason: str | None = None


class _RateLimitedSdk:
    def __init__(
        self,
        sdk: _SessionSdk,
        activity: Callable[[Literal["started", "completed"]], None],
        interval_seconds: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.__version__ = sdk.__version__
        self._sdk = sdk
        self._activity = activity
        self._interval_seconds = interval_seconds
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_started: float | None = None

    def query_trade_dates(self, *, start_date: str, end_date: str) -> BaoStockRowResult:
        return self._call(lambda: self._sdk.query_trade_dates(start_date=start_date, end_date=end_date))

    def query_stock_basic(self) -> BaoStockRowResult:
        return self._call(self._sdk.query_stock_basic)

    def query_stock_industry(self, *, code: str = "", date: str = "") -> BaoStockRowResult:
        return self._call(lambda: self._sdk.query_stock_industry(code=code, date=date))

    def query_history_k_data_plus(  # noqa: PLR0913
        self,
        code: str,
        fields: str,
        start_date: str,
        end_date: str,
        *,
        frequency: str,
        adjustflag: str,
    ) -> BaoStockRowResult:
        return self._call(
            lambda: self._sdk.query_history_k_data_plus(
                code,
                fields,
                start_date,
                end_date,
                frequency=frequency,
                adjustflag=adjustflag,
            )
        )

    def _call(self, call: Callable[[], BaoStockRowResult]) -> BaoStockRowResult:
        now = self._monotonic()
        if self._last_started is not None:
            remaining = self._interval_seconds - (now - self._last_started)
            if remaining > 0:
                self._sleep(remaining)
                now = self._monotonic()
        self._last_started = now
        self._activity("started")
        try:
            return call()
        finally:
            self._activity("completed")


class BaoStockHistorySupplier:
    """Own one long-lived SDK child with bounded calls and retries."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 45.0,
        retries: int = 2,
        query_interval_seconds: float = 2.0,
        cancellation_grace_seconds: float = 10.0,
    ) -> None:
        if (
            timeout_seconds <= 0
            or not 0 <= retries <= 2
            or query_interval_seconds < 2.0
            or not 0 < cancellation_grace_seconds <= 10.0
        ):
            raise ValueError("BaoStock supplier bounds are invalid")
        self._timeout_seconds = timeout_seconds
        self._retries = retries
        self._query_interval_seconds = query_interval_seconds
        self._cancellation_grace_seconds = cancellation_grace_seconds
        self._process: BaseProcess | None = None
        self._connection: Connection | None = None

    def __enter__(self) -> BaoStockHistorySupplier:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def load_context(self, as_of: date, sessions: int) -> HistorySupplierContext:
        response = self._request(_LoadContext(as_of, sessions))
        if response.context is None:
            raise RuntimeError(response.failure_reason or "supplier_context_failed")
        return response.context

    def fetch_code(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload:
        response = self._request(_FetchCode(security, dates))
        if response.download is None:
            raise RuntimeError(response.failure_reason or "supplier_query_failed")
        return response.download

    def close(self) -> None:
        process = self._process
        connection = self._connection
        self._process = None
        self._connection = None
        if connection is not None:
            if process is not None and process.is_alive():
                try:
                    connection.send(_Stop())
                    process.join(timeout=self._cancellation_grace_seconds)
                except (BrokenPipeError, OSError):
                    pass
            connection.close()
        if process is not None:
            _terminate(process)

    def _request(self, command: _LoadContext | _FetchCode) -> _Response:
        failure = "supplier_process_failed"
        for _attempt in range(self._retries + 1):
            try:
                self._start()
                connection = self._require_connection()
                connection.send(command)
                deadline = time.monotonic() + self._timeout_seconds
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not connection.poll(min(remaining, 0.1)):
                        if remaining <= 0:
                            failure = "supplier_call_timeout"
                            break
                        continue
                    response = connection.recv()
                    if isinstance(response, _Activity):
                        deadline = time.monotonic() + self._timeout_seconds
                        continue
                    if isinstance(response, _Response):
                        if response.failure_reason is None:
                            return response
                        failure = response.failure_reason
                        break
                    failure = "supplier_protocol_invalid"
                    break
            except (EOFError, OSError):
                failure = "supplier_process_failed"
            self.close()
        raise RuntimeError(failure)

    def _start(self) -> None:
        if self._process is not None and self._process.is_alive() and self._connection is not None:
            return
        self.close()
        process_context = get_context("spawn")
        parent, child = process_context.Pipe()
        process = process_context.Process(
            target=_worker_main,
            args=(child, self._query_interval_seconds),
            daemon=True,
        )
        process.start()
        child.close()
        self._process = process
        self._connection = parent
        if not parent.poll(self._timeout_seconds):
            self.close()
            raise RuntimeError("supplier_login_timeout")
        response = parent.recv()
        if not isinstance(response, _Ready) or response.failure_reason is not None:
            self.close()
            raise RuntimeError(response.failure_reason if isinstance(response, _Ready) else "supplier_protocol_invalid")

    def _require_connection(self) -> Connection:
        if self._connection is None:
            raise RuntimeError("supplier_process_failed")
        return self._connection


def _worker_main(connection: Connection, query_interval_seconds: float) -> None:
    _silence_vendor_output()
    sdk: _SessionSdk | None = None
    try:
        sdk = _load_sdk()
        _login(sdk)
        gateway = BaoStockRowGateway(
            _RateLimitedSdk(
                sdk,
                lambda state: connection.send(_Activity(state)),
                query_interval_seconds,
            ),
            python_version=platform.python_version(),
            dependency_versions=_dependency_versions(),
        )
        connection.send(_Ready())
        while True:
            command = connection.recv()
            if isinstance(command, _Stop):
                return
            try:
                if isinstance(command, _LoadContext):
                    requested = BaoStockDailySpec(sessions=command.sessions, source_cutoff=command.as_of)
                    calendar = gateway.fetch_calendar(requested)
                    spec = BaoStockDailySpec(sessions=command.sessions, source_cutoff=calendar.open_dates[-1])
                    universe = gateway.fetch_universe(spec)
                    industries = gateway.fetch_industry_intervals(spec, calendar, universe)
                    connection.send(
                        _Response(HistorySupplierContext(calendar, universe, gateway.source_versions(), industries))
                    )
                elif isinstance(command, _FetchCode):
                    calendar = BaoStockCalendar(command.dates)
                    spec = BaoStockDailySpec(sessions=len(command.dates), source_cutoff=command.dates[-1])
                    connection.send(_Response(download=gateway.fetch_code_download(spec, command.security, calendar)))
                else:
                    connection.send(_Response(failure_reason="supplier_protocol_invalid"))
            except Exception as exc:
                connection.send(_Response(failure_reason=_failure_code(exc)))
    except Exception as exc:
        connection.send(_Ready(_failure_code(exc)))
    finally:
        if sdk is not None:
            _logout(sdk)
        connection.close()


def _load_sdk() -> _SessionSdk:
    try:
        import baostock
    except ImportError as exc:
        raise RuntimeError("dependency_unavailable") from exc
    return cast(_SessionSdk, baostock)


def _login(sdk: _SessionSdk) -> None:
    try:
        result = sdk.login()
    except TimeoutError as exc:
        raise RuntimeError("supplier_login_timeout") from exc
    except OSError as exc:
        raise RuntimeError("supplier_login_network_failed") from exc
    except Exception as exc:
        raise RuntimeError("supplier_login_sdk_failed") from exc
    if str(result.error_code) != "0":
        raise RuntimeError("supplier_login_rejected")


def _logout(sdk: _SessionSdk) -> None:
    try:
        sdk.logout()
    except Exception:
        pass


def _dependency_versions() -> tuple[tuple[str, str], ...]:
    values = []
    for package in ("baostock", "pandas"):
        try:
            values.append((package, importlib.metadata.version(package)))
        except importlib.metadata.PackageNotFoundError:
            values.append((package, "not-installed"))
    return tuple(values)


def _silence_vendor_output() -> None:
    descriptor = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(descriptor, 1)
        os.dup2(descriptor, 2)
    finally:
        os.close(descriptor)


def _failure_code(exc: BaseException) -> str:
    value = str(exc).strip()
    return value if value and len(value) <= 64 and value.replace("_", "").isalnum() else "supplier_failed"


def _terminate(process: BaseProcess) -> None:
    if process.is_alive():
        process.terminate()
    process.join(timeout=1.0)
    if process.is_alive() and hasattr(process, "kill"):
        process.kill()
        process.join(timeout=1.0)


__all__ = ["BaoStockHistorySupplier"]
