"""Single-process BaoStock session for zero-argument history synchronization."""

from __future__ import annotations

import importlib.metadata
import os
import platform
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
from multiprocessing import get_context
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Literal, Protocol, cast

from trader.application.research.history_sync import (
    HistorySupplierContext,
    HistorySyncConfiguration,
    HistorySyncProgress,
    HistorySyncProgressPort,
    HistorySyncProgressStage,
)
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
    stage: HistorySyncProgressStage
    state: Literal["started", "returned"]
    current_item: str | None = None


@dataclass(frozen=True)
class _Attempt:
    number: int
    total: int


@dataclass(frozen=True)
class _RequestState:
    stage: HistorySyncProgressStage
    current_item: str | None
    call_started_at: float
    deadline: float = 0.0
    next_heartbeat: float = 0.0
    worker_ready: bool = False


@dataclass(frozen=True)
class _AttemptResult:
    response: _Response | None
    state: _RequestState
    failure_reason: str | None


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
        activity: Callable[[HistorySyncProgressStage, Literal["started", "returned"], str | None], None],
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
        return self._call(
            "supplier_calendar",
            f"{start_date}:{end_date}",
            lambda: self._sdk.query_trade_dates(start_date=start_date, end_date=end_date),
        )

    def query_stock_basic(self) -> BaoStockRowResult:
        return self._call("supplier_universe", None, self._sdk.query_stock_basic)

    def query_stock_industry(self, *, code: str = "", date: str = "") -> BaoStockRowResult:
        return self._call(
            "supplier_industry",
            code or date or None,
            lambda: self._sdk.query_stock_industry(code=code, date=date),
        )

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
        stage: HistorySyncProgressStage = "supplier_daily_raw" if adjustflag == "3" else "supplier_daily_qfq"
        return self._call(
            stage,
            code,
            lambda: self._sdk.query_history_k_data_plus(
                code,
                fields,
                start_date,
                end_date,
                frequency=frequency,
                adjustflag=adjustflag,
            ),
        )

    def _call(
        self,
        stage: HistorySyncProgressStage,
        current_item: str | None,
        call: Callable[[], BaoStockRowResult],
    ) -> BaoStockRowResult:
        now = self._monotonic()
        if self._last_started is not None:
            remaining = self._interval_seconds - (now - self._last_started)
            if remaining > 0:
                self._sleep(remaining)
                now = self._monotonic()
        self._last_started = now
        self._activity(stage, "started", current_item)
        try:
            return call()
        finally:
            self._activity(stage, "returned", current_item)


class BaoStockHistorySupplier:
    """Own one long-lived SDK child with bounded calls and retries."""

    def __init__(
        self,
        configuration: HistorySyncConfiguration | None = None,
        *,
        progress: HistorySyncProgressPort | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        settings = configuration or HistorySyncConfiguration()
        self._timeout_seconds = settings.supplier_timeout_seconds
        self._retries = settings.supplier_retries
        self._query_interval_seconds = settings.query_interval_seconds
        self._cancellation_grace_seconds = settings.cancellation_grace_seconds
        self._heartbeat_interval_seconds = settings.progress_heartbeat_seconds
        self._progress = progress
        self._monotonic = monotonic
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
        max_attempts = self._retries + 1
        initial_stage, initial_item = _command_progress(command)
        for attempt_number in range(1, max_attempts + 1):
            attempt = _Attempt(attempt_number, max_attempts)
            result = self._request_once(
                command,
                _RequestState(initial_stage, initial_item, self._monotonic()),
                attempt,
            )
            state = result.state
            failure = result.failure_reason or ""
            if result.response is not None and not failure:
                self._publish(state.stage, "completed", attempt, state.current_item, self._elapsed(state))
                return result.response
            self._publish(state.stage, "failed", attempt, state.current_item, self._elapsed(state))
            self.close()
            if attempt.number < attempt.total:
                next_attempt = _Attempt(attempt.number + 1, attempt.total)
                self._publish(state.stage, "retrying", next_attempt, state.current_item, 0.0)
        raise RuntimeError(failure)

    def _request_once(
        self,
        command: _LoadContext | _FetchCode,
        state: _RequestState,
        attempt: _Attempt,
    ) -> _AttemptResult:
        try:
            self._start(attempt)
            state = replace(state, worker_ready=True)
            connection = self._require_connection()
            connection.send(command)
            started_at = self._monotonic()
            state = replace(
                state,
                call_started_at=started_at,
                deadline=started_at + self._timeout_seconds,
                next_heartbeat=started_at + self._heartbeat_interval_seconds,
            )
            self._publish(state.stage, "started", attempt, state.current_item, 0.0)
            while True:
                response, state = self._receive(connection, state, attempt)
                if response is None:
                    continue
                if isinstance(response, _Activity):
                    state = self._handle_activity(response, state, attempt)
                    continue
                if not isinstance(response, _Response):
                    raise RuntimeError("supplier_protocol_invalid")
                return _AttemptResult(response, state, response.failure_reason)
        except (EOFError, OSError, RuntimeError) as exc:
            if not state.worker_ready:
                state = replace(state, stage="supplier_login", current_item=None)
            return _AttemptResult(None, state, _failure_code(exc))

    def _receive(
        self,
        connection: Connection,
        state: _RequestState,
        attempt: _Attempt,
    ) -> tuple[object | None, _RequestState]:
        now = self._monotonic()
        remaining = state.deadline - now
        if remaining <= 0:
            raise RuntimeError(f"{state.stage}_timeout")
        poll_seconds = min(remaining, max(0.0, state.next_heartbeat - now), 0.1)
        if connection.poll(poll_seconds):
            return cast(object, connection.recv()), state
        now = self._monotonic()
        if now >= state.next_heartbeat:
            self._publish(state.stage, "waiting", attempt, state.current_item, self._elapsed(state))
            state = replace(state, next_heartbeat=now + self._heartbeat_interval_seconds)
        return None, state

    def _handle_activity(
        self,
        activity: _Activity,
        state: _RequestState,
        attempt: _Attempt,
    ) -> _RequestState:
        now = self._monotonic()
        if activity.state == "started":
            state = replace(
                state,
                stage=activity.stage,
                current_item=activity.current_item,
                call_started_at=now,
                deadline=now + self._timeout_seconds,
            )
            self._publish(state.stage, "started", attempt, state.current_item, 0.0)
        else:
            state = replace(state, stage=activity.stage, current_item=activity.current_item)
            self._publish(state.stage, "waiting", attempt, state.current_item, self._elapsed(state))
        return replace(state, next_heartbeat=now + self._heartbeat_interval_seconds)

    def _elapsed(self, state: _RequestState) -> float:
        return max(0.0, self._monotonic() - state.call_started_at)

    def _start(self, attempt: _Attempt) -> None:
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
        started_at = self._monotonic()
        deadline = started_at + self._timeout_seconds
        next_heartbeat = started_at + self._heartbeat_interval_seconds
        self._publish("supplier_login", "started", attempt, None, 0.0)
        while not parent.poll(min(0.1, max(0.0, min(deadline, next_heartbeat) - self._monotonic()))):
            now = self._monotonic()
            if now >= deadline:
                self.close()
                raise RuntimeError("supplier_login_timeout")
            if now >= next_heartbeat:
                self._publish("supplier_login", "waiting", attempt, None, now - started_at)
                next_heartbeat = now + self._heartbeat_interval_seconds
        response = parent.recv()
        if not isinstance(response, _Ready) or response.failure_reason is not None:
            self.close()
            raise RuntimeError(response.failure_reason if isinstance(response, _Ready) else "supplier_protocol_invalid")
        self._publish(
            "supplier_login",
            "completed",
            attempt,
            None,
            self._monotonic() - started_at,
        )

    def _require_connection(self) -> Connection:
        if self._connection is None:
            raise RuntimeError("supplier_process_failed")
        return self._connection

    def _publish(
        self,
        stage: HistorySyncProgressStage,
        state: Literal["started", "waiting", "retrying", "completed", "failed"],
        attempt: _Attempt,
        current_item: str | None,
        call_elapsed_seconds: float,
    ) -> None:
        if self._progress is None:
            return
        try:
            self._progress.publish(
                HistorySyncProgress(
                    stage,
                    state,
                    1 if state == "completed" else 0,
                    1,
                    current_item=current_item,
                    attempt=attempt.number,
                    max_attempts=attempt.total,
                    call_elapsed_seconds=max(0.0, call_elapsed_seconds),
                )
            )
        except OSError:
            pass


def _worker_main(connection: Connection, query_interval_seconds: float) -> None:
    _silence_vendor_output()
    sdk: _SessionSdk | None = None
    try:
        sdk = _load_sdk()
        _login(sdk)
        gateway = BaoStockRowGateway(
            _RateLimitedSdk(
                sdk,
                lambda stage, state, current_item: connection.send(_Activity(stage, state, current_item)),
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


def _command_progress(command: _LoadContext | _FetchCode) -> tuple[HistorySyncProgressStage, str | None]:
    if isinstance(command, _LoadContext):
        return "supplier_calendar", None
    return "supplier_daily_raw", command.security.code


def _terminate(process: BaseProcess) -> None:
    if process.is_alive():
        process.terminate()
    process.join(timeout=1.0)
    if process.is_alive() and hasattr(process, "kill"):
        process.kill()
        process.join(timeout=1.0)


__all__ = ["BaoStockHistorySupplier"]
