"""Shared BaoStock SDK session mechanics for active research adapters."""

from __future__ import annotations

import importlib.metadata
import time
from collections.abc import Callable, Sequence
from typing import Literal, Protocol, TypeVar, cast

from trader.download.domain.history_sync import (
    BAOSTOCK_MIN_QUERY_INTERVAL_SECONDS,
    BAOSTOCK_PREPARATION_INTERVAL_SECONDS,
)
from trader.download.infra.baostock_gateway import BaoStockRowResult, BaoStockSdkPort


class BaoStockSessionSdkPort(BaoStockSdkPort, Protocol):
    def login(self) -> BaoStockRowResult: ...

    def logout(self) -> BaoStockRowResult: ...


_Result = TypeVar("_Result")
_SDK_PAGE_ROWS = 2000


class BaoStockQueryLimiter:
    """Pace calls and SDK-owned pagination on the same session timeline."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        after_login: bool = False,
    ) -> None:
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_started = monotonic() if after_login else None
        self._last_interval = BAOSTOCK_PREPARATION_INTERVAL_SECONDS if after_login else 0.0

    def protect_after_login(self) -> None:
        self._last_started = self._monotonic()
        self._last_interval = BAOSTOCK_PREPARATION_INTERVAL_SECONDS

    def call(
        self,
        call: Callable[[], _Result],
        interval: float,
        activity: Callable[[Literal["started", "returned"]], None],
    ) -> _Result:
        now = self._monotonic()
        if self._last_started is not None:
            remaining = max(interval, self._last_interval) - (now - self._last_started)
            if remaining > 0:
                self._sleep(remaining)
                now = self._monotonic()
        self._last_started = now
        self._last_interval = interval
        activity("started")
        try:
            return call()
        finally:
            activity("returned")

    def query(
        self,
        call: Callable[[], BaoStockRowResult],
        interval: float,
        activity: Callable[[Literal["started", "returned"]], None],
    ) -> BaoStockRowResult:
        return _PagedBaoStockRows(
            self.call(call, interval, activity), lambda next_page: self.call(next_page, interval, activity)
        )


class _PagedBaoStockRows:
    """Guard the SDK's implicit next-page request at its 2000-row boundary."""

    def __init__(self, result: BaoStockRowResult, next_page: Callable[[Callable[[], bool]], bool]) -> None:
        self._result = result
        self._next_page = next_page
        self._page_rows = 0

    @property
    def error_code(self) -> str:
        return self._result.error_code

    @property
    def error_msg(self) -> str:
        return self._result.error_msg

    @property
    def fields(self) -> Sequence[str]:
        return self._result.fields

    def next(self) -> bool:
        if self._page_rows == _SDK_PAGE_ROWS:
            self._page_rows = 0
            return self._next_page(self._result.next)
        return self._result.next()

    def get_row_data(self) -> Sequence[str]:
        self._page_rows += 1
        return self._result.get_row_data()


class RateLimitedBaoStockSdk:
    """Keep every SDK query on one socket and bound the call start rate."""

    def __init__(
        self,
        sdk: BaoStockSessionSdkPort,
        *,
        interval_seconds: float = BAOSTOCK_MIN_QUERY_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        activity: Callable[[Literal["started", "completed"]], None] | None = None,
    ) -> None:
        if interval_seconds <= 0.0:
            raise ValueError("BaoStock query interval must be positive")
        self.__version__ = sdk.__version__
        self._sdk = sdk
        self._interval_seconds = interval_seconds
        self._limiter = BaoStockQueryLimiter(monotonic=monotonic, sleep=sleep)
        self._activity = activity or (lambda _state: None)

    def protect_after_login(self) -> None:
        self._limiter.protect_after_login()

    def query_trade_dates(self, *, start_date: str, end_date: str) -> BaoStockRowResult:
        return self._query(lambda: self._sdk.query_trade_dates(start_date=start_date, end_date=end_date))

    def query_stock_basic(self) -> BaoStockRowResult:
        return self._query(self._sdk.query_stock_basic)

    def query_stock_industry(self, *, code: str = "", date: str = "") -> BaoStockRowResult:
        return self._query(lambda: self._sdk.query_stock_industry(code=code, date=date))

    def query_history_k_data_plus(  # noqa: PLR0913 - exact third-party signature
        self,
        code: str,
        fields: str,
        start_date: str,
        end_date: str,
        *,
        frequency: str,
        adjustflag: str,
    ) -> BaoStockRowResult:
        return self._query(
            lambda: self._sdk.query_history_k_data_plus(
                code,
                fields,
                start_date,
                end_date,
                frequency=frequency,
                adjustflag=adjustflag,
            )
        )

    def _query(self, call: Callable[[], BaoStockRowResult]) -> BaoStockRowResult:
        return self._limiter.query(
            call, self._interval_seconds, lambda state: self._activity("started" if state == "started" else "completed")
        )


def load_baostock_sdk() -> BaoStockSessionSdkPort:
    try:
        import baostock
    except ImportError as exc:
        raise RuntimeError("dependency_unavailable") from exc
    return cast(BaoStockSessionSdkPort, baostock)


def login_baostock(sdk: BaoStockSessionSdkPort) -> None:
    try:
        result = sdk.login()
    except PermissionError as exc:
        raise RuntimeError("supplier_login_network_denied") from exc
    except TimeoutError as exc:
        raise RuntimeError("supplier_login_timeout") from exc
    except UnboundLocalError as exc:
        raise RuntimeError("supplier_login_transport_failed") from exc
    except OSError as exc:
        raise RuntimeError("supplier_login_network_failed") from exc
    except Exception as exc:
        raise RuntimeError("supplier_login_sdk_failed") from exc
    error_code = str(result.error_code)
    if error_code == "0":
        return
    if error_code == "10001011":
        raise RuntimeError("supplier_login_failed_blacklisted")
    if error_code.isascii() and error_code.isalnum():
        raise RuntimeError(f"supplier_login_rejected_{error_code[:24]}")
    raise RuntimeError("supplier_login_rejected")


def logout_baostock(sdk: BaoStockSessionSdkPort) -> None:
    try:
        sdk.logout()
    except Exception:
        pass


def baostock_dependency_versions() -> tuple[tuple[str, str], ...]:
    values: list[tuple[str, str]] = []
    for package in ("baostock", "pandas"):
        try:
            values.append((package, importlib.metadata.version(package)))
        except importlib.metadata.PackageNotFoundError:
            values.append((package, "not-installed"))
    return tuple(values)


__all__ = [
    "BaoStockQueryLimiter",
    "BaoStockSessionSdkPort",
    "RateLimitedBaoStockSdk",
    "baostock_dependency_versions",
    "load_baostock_sdk",
    "login_baostock",
    "logout_baostock",
]
