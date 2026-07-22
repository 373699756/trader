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


class RouteNoData(MarketDataNoDataError):
    """`MarketDataNoDataError` carrying diagnostic routing outcome."""

    def __init__(self, message: str, outcome: RouteOutcome) -> None:
        super().__init__(message)
        self.route_outcome = outcome


class RouteFailed(MarketDataFailedError):
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
    outcomes: list[VendorResult] = []
    failures: list[tuple[str, str]] = []
    no_data: list[tuple[str, str]] = []
    degraded = False
    for route_item in routes:
        start = perf_counter()
        try:
            value: object = route_item.fetch()
        except MarketDataNoDataError as exc:
            message = _strip_vendor_prefix(route_item.name, str(exc)[:500] or on_no_data)
            skipped = route_item.severity is VendorSeverity.OPTIONAL
            status = "skipped" if skipped else "no_data"
            if route_item.severity is VendorSeverity.REQUIRED:
                no_data.append((route_item.name, message))
                degraded = True
                outcomes.append(
                    VendorResult(
                        name=route_item.name,
                        status="no_data",
                        severity=route_item.severity,
                        error=message,
                        duration_ms=_elapsed_ms(start),
                    )
                )
                continue
            failures.append((route_item.name, message))
            degraded = True
            outcomes.append(
                VendorResult(
                    name=route_item.name,
                    status=status,
                    severity=route_item.severity,
                    error=message,
                    skipped=skipped,
                    duration_ms=_elapsed_ms(start),
                )
            )
            continue
        except Exception as exc:
            message = _strip_vendor_prefix(route_item.name, str(exc)[:500])
            skipped = _is_skipped_error(message)
            failures.append((route_item.name, message))
            if route_item.severity is VendorSeverity.REQUIRED:
                degraded = True
                outcomes.append(
                    VendorResult(
                        name=route_item.name,
                        status="skipped" if skipped else "failed",
                        severity=route_item.severity,
                        error=message,
                        skipped=skipped,
                        duration_ms=_elapsed_ms(start),
                    )
                )
                continue
            degraded = True
            outcomes.append(
                VendorResult(
                    name=route_item.name,
                    status="skipped" if skipped else "failed",
                    severity=route_item.severity,
                    error=message,
                    skipped=skipped,
                    duration_ms=_elapsed_ms(start),
                )
            )
            continue
        if _is_empty_payload(value):
            if route_item.severity is VendorSeverity.REQUIRED:
                no_data.append((route_item.name, on_no_data))
                degraded = True
                status = "no_data"
                skipped = False
                outcomes.append(
                    VendorResult(
                        name=route_item.name,
                        status=status,
                        severity=route_item.severity,
                        error=on_no_data,
                        duration_ms=_elapsed_ms(start),
                    )
                )
                continue
            skipped = True
            failures.append((route_item.name, on_no_data))
            degraded = True
            outcomes.append(
                VendorResult(
                    name=route_item.name,
                    status="skipped",
                    severity=route_item.severity,
                    error=on_no_data,
                    skipped=skipped,
                    duration_ms=_elapsed_ms(start),
                )
            )
            continue
        outcomes.append(
            VendorResult(
                name=route_item.name,
                status="success",
                severity=route_item.severity,
                result=value,
                duration_ms=_elapsed_ms(start),
            )
        )
        return RouteOutcome(
            result=value,
            vendor=route_item.name,
            results=tuple(outcomes),
            degraded=degraded,
            status="success",
        )

    if no_data:
        detail = "; ".join(f"{name}: {error}" for name, error in no_data)
        if failures:
            detail = f"{detail}; upstream failures: {'; '.join(f'{name}: {error}' for name, error in failures)}"
        outcome = RouteOutcome(
            result=None,
            vendor="",
            results=tuple(outcomes),
            degraded=degraded,
            status="no_data",
            fallback_reason="no_data",
        )
        raise RouteNoData(detail or on_no_data, outcome)

    if failures:
        outcome = RouteOutcome(
            result=None,
            vendor=failures[-1][0],
            results=tuple(outcomes),
            degraded=degraded,
            status="failed",
            fallback_reason="failed",
        )
        raise RouteFailed(
            failures[-1][0],
            "; ".join(f"{name}: {error}" for name, error in failures),
            outcome,
        )

    outcome = RouteOutcome(
        result=None,
        vendor="unavailable",
        results=tuple(outcomes),
        degraded=True,
        status="failed",
        fallback_reason="failed",
    )
    raise RouteFailed("all_vendors", "all vendors exhausted", outcome)


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
    "RouteFailed",
    "RouteNoData",
    "RouteOutcome",
    "VendorResult",
    "VendorRoute",
    "VendorSeverity",
    "route",
]
