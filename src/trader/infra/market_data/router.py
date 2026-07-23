"""Vendor routing table with required/optional fallback semantics.

Inspired by TradingAgents ``dataflows/interface.py`` VENDOR_METHODS pattern:
method name maps to an ordered list of (vendor_name, fetch_fn) tuples with a
``required`` flag.  Required vendors raise on failure; optional vendors
return a sentinel that callers can handle via cache fallback or degradation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from time import perf_counter
from typing import NoReturn

from trader.application.ports.market import MarketDataFailedError, MarketDataNoDataError


class VendorSeverity(str, Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"


@dataclass(frozen=True)
class VendorRoute:
    name: str
    fetch: Callable[[], object]
    severity: VendorSeverity


@dataclass
class VendorResult:
    name: str
    status: str
    severity: VendorSeverity
    result: object | None = None
    error: str = ""
    skipped: bool = False
    duration_ms: float | None = None


@dataclass
class RouteOutcome:
    result: object | None
    vendor: str
    results: tuple[VendorResult, ...]
    degraded: bool = False
    status: str = "success"
    fallback_reason: str | None = None


@dataclass(frozen=True)
class _VendorAttempt:
    result: VendorResult
    value: object | None
    failure_kind: str = ""


class RouteNoDataError(MarketDataNoDataError):
    """`MarketDataNoDataError` carrying diagnostic routing outcome."""

    def __init__(self, message: str, outcome: RouteOutcome) -> None:
        super().__init__(message)
        self.route_outcome = outcome


class RouteFailedError(MarketDataFailedError):
    """`MarketDataFailedError` carrying diagnostic routing outcome."""

    def __init__(self, vendor: str, error: str, outcome: RouteOutcome) -> None:
        super().__init__(vendor, error)
        self.route_outcome = outcome


def route(
    routes: Sequence[VendorRoute],
    *,
    on_no_data: str = "no_data_from_vendor",
) -> RouteOutcome:
    """Execute *routes* in order, stopping on first success.

    Required vendors that fail or return empty/no data are recorded and allow
    fallback to the next vendor.  Optional vendors record degradations and do
    not break the chain.

    Failures are represented as ``MarketDataFailedError``.  Empty/no-data results
    are represented as ``MarketDataNoDataError`` when all required routes are
    exhausted.
    """
    results: list[VendorResult] = []
    failures: list[tuple[str, str]] = []
    no_data: list[tuple[str, str]] = []
    degraded = False
    for route_item in routes:
        attempt = _attempt_vendor(route_item, on_no_data)
        results.append(attempt.result)
        if not attempt.failure_kind:
            return RouteOutcome(
                result=attempt.value,
                vendor=route_item.name,
                results=tuple(results),
                degraded=degraded,
                status="success",
            )
        degraded = True
        failure = (route_item.name, attempt.result.error)
        (no_data if attempt.failure_kind == "no_data" else failures).append(failure)

    _raise_route_failure(results, failures, no_data, degraded, on_no_data)


def _attempt_vendor(route_item: VendorRoute, on_no_data: str) -> _VendorAttempt:
    start = perf_counter()
    try:
        value = route_item.fetch()
    except MarketDataNoDataError as exc:
        message = _strip_vendor_prefix(route_item.name, str(exc)[:500] or on_no_data)
        return _failed_attempt(route_item, message, start, no_data=True)
    except Exception as exc:
        message = _strip_vendor_prefix(route_item.name, str(exc)[:500])
        return _failed_attempt(route_item, message, start)
    if _is_empty_payload(value):
        return _failed_attempt(route_item, on_no_data, start, no_data=True)
    return _VendorAttempt(
        VendorResult(
            name=route_item.name,
            status="success",
            severity=route_item.severity,
            result=value,
            duration_ms=_elapsed_ms(start),
        ),
        value,
    )


def _failed_attempt(
    route_item: VendorRoute,
    message: str,
    start: float,
    *,
    no_data: bool = False,
) -> _VendorAttempt:
    required_no_data = no_data and route_item.severity is VendorSeverity.REQUIRED
    skipped = (no_data and route_item.severity is VendorSeverity.OPTIONAL) or _is_skipped_error(message)
    status = "no_data" if required_no_data else "skipped" if skipped else "failed"
    return _VendorAttempt(
        VendorResult(
            name=route_item.name,
            status=status,
            severity=route_item.severity,
            error=message,
            skipped=skipped,
            duration_ms=_elapsed_ms(start),
        ),
        None,
        "no_data" if required_no_data else "failure",
    )


def _raise_route_failure(
    results: Sequence[VendorResult],
    failures: Sequence[tuple[str, str]],
    no_data: Sequence[tuple[str, str]],
    degraded: bool,
    on_no_data: str,
) -> NoReturn:
    if no_data:
        detail = "; ".join(f"{name}: {error}" for name, error in no_data)
        if failures:
            detail = f"{detail}; upstream failures: {'; '.join(f'{name}: {error}' for name, error in failures)}"
        outcome = RouteOutcome(None, "", tuple(results), degraded, "no_data", "no_data")
        raise RouteNoDataError(detail or on_no_data, outcome)
    if failures:
        vendor = failures[-1][0]
        outcome = RouteOutcome(None, vendor, tuple(results), degraded, "failed", "failed")
        raise RouteFailedError(vendor, "; ".join(f"{name}: {error}" for name, error in failures), outcome)
    outcome = RouteOutcome(None, "unavailable", tuple(results), True, "failed", "failed")
    raise RouteFailedError("all_vendors", "all vendors exhausted", outcome)


def _is_empty_payload(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, Mapping):
        return len(value) == 0
    if isinstance(value, (bytes, bytearray, str)):
        return False
    return isinstance(value, Sequence) and len(value) == 0


def _elapsed_ms(start: float) -> float:
    return (perf_counter() - start) * 1000.0


def _strip_vendor_prefix(vendor: str, message: str) -> str:
    prefix = f"{vendor}:"
    if message.startswith(prefix):
        return message[len(prefix) :].lstrip()
    return message


def _is_skipped_error(message: str) -> bool:
    return message == "circuit_open"


__all__ = [
    "RouteFailedError",
    "RouteNoDataError",
    "RouteOutcome",
    "VendorResult",
    "VendorRoute",
    "VendorSeverity",
    "route",
]
