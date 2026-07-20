"""Market-data source fallback, single-flight and circuit-breaker owner."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Generic, TypeVar, cast

from trader.application.ports import MarketDataFailed, MarketDataNoData, MarketDataUnavailable
from trader.domain.models import MarketQuote
from trader.infrastructure.market_data.eastmoney import EastmoneyClient
from trader.infrastructure.market_data.router import RouteOutcome, VendorRoute, VendorSeverity, route
from trader.infrastructure.market_data.sina import SinaClient
from trader.infrastructure.market_data.tencent import TencentClient

_T = TypeVar("_T")


@dataclass
class _CircuitState:
    failures: int = 0
    open_until: float = 0.0
    success_count: int = 0
    error_count: int = 0
    last_latency_ms: float = 0.0
    last_error: str = ""
    planned_count: int = 0
    latencies_ms: deque[float] = field(default_factory=lambda: deque(maxlen=256))


class _SingleFlight(Generic[_T]):
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._running = False
        self._generation = 0
        self._result: _T | None = None
        self._error: BaseException | None = None

    def run(self, function: Callable[[], _T]) -> _T:
        with self._condition:
            generation = self._generation
            if self._running:
                while self._running and self._generation == generation:
                    self._condition.wait()
                if self._error is not None:
                    raise self._error
                return cast(_T, self._result)
            self._running = True
        try:
            result = function()
        except BaseException as exc:
            with self._condition:
                self._error = exc
                self._result = None
                self._running = False
                self._generation += 1
                self._condition.notify_all()
            raise
        with self._condition:
            self._result = result
            self._error = None
            self._running = False
            self._generation += 1
            self._condition.notify_all()
        return result


class MarketDataGateway:
    def __init__(
        self,
        eastmoney: EastmoneyClient,
        sina: SinaClient,
        tencent: TencentClient,
        *,
        minimum_market_rows: int,
        circuit_breaker_failures: int,
        circuit_breaker_seconds: int,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._eastmoney = eastmoney
        self._sina = sina
        self._tencent = tencent
        self._minimum_market_rows = minimum_market_rows
        self._failure_limit = circuit_breaker_failures
        self._breaker_seconds = circuit_breaker_seconds
        self._monotonic = monotonic
        self._market_flight: _SingleFlight[Sequence[MarketQuote]] = _SingleFlight()
        self._candidate_fetch_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._states = {"eastmoney": _CircuitState(), "sina": _CircuitState(), "tencent": _CircuitState()}
        self._latest_by_code: dict[str, MarketQuote] = {}
        self._latest_source = "unavailable"
        self._last_route_outcome: RouteOutcome | None = None

    def fetch_market(self) -> Sequence[MarketQuote]:
        return self._market_flight.run(self._fetch_market_once)

    def _fetch_market_once(self) -> Sequence[MarketQuote]:
        vendor_routes = (
            VendorRoute(
                "eastmoney",
                lambda: self._fetch_from_vendor("eastmoney", self._eastmoney.fetch_market),
                VendorSeverity.REQUIRED,
            ),
            VendorRoute(
                "sina", lambda: self._fetch_from_vendor("sina", self._sina.fetch_market), VendorSeverity.REQUIRED
            ),
        )
        try:
            outcome = route(vendor_routes, on_no_data="insufficient rows")
        except (MarketDataFailed, MarketDataNoData) as exc:
            outcome = _route_outcome_from_exception(exc)
            with self._state_lock:
                self._last_route_outcome = outcome
            with self._state_lock:
                cached = tuple(self._latest_by_code.values())
            if cached:
                return cached
            raise MarketDataUnavailable("market data unavailable: " + str(exc)) from exc
        quotes = tuple(cast(Sequence[MarketQuote], outcome.result))
        with self._state_lock:
            for quote in quotes:
                current = self._latest_by_code.get(quote.code)
                if current is None or _quote_version(quote) > _quote_version(current):
                    self._latest_by_code[quote.code] = quote
            self._latest_source = outcome.vendor
            self._last_route_outcome = outcome
            return tuple(self._latest_by_code.values())

    def fetch_candidates(self, codes: Sequence[str]) -> Sequence[MarketQuote]:
        if not codes:
            return ()
        with self._candidate_fetch_lock:
            self._record_planned("tencent")
            if self._is_open("tencent"):
                self._record_skipped_open("tencent")
                with self._state_lock:
                    return tuple(self._latest_by_code[code] for code in codes if code in self._latest_by_code)
            started = self._monotonic()
            try:
                targeted = tuple(self._tencent.fetch_quotes(codes))
            except Exception as exc:
                self._record("tencent", False, started, str(exc))
                with self._state_lock:
                    return tuple(self._latest_by_code[code] for code in codes if code in self._latest_by_code)
            self._record("tencent", True, started, "")
            with self._state_lock:
                baseline = dict(self._latest_by_code)
            verified: list[MarketQuote] = []
            for quote in targeted:
                previous = baseline.get(quote.code)
                deviation = _price_deviation_pct(previous.price if previous else None, quote.price)
                verified.append(
                    replace(
                        quote,
                        industry=previous.industry if previous and not quote.industry else quote.industry,
                        market_cap=previous.market_cap if previous and quote.market_cap is None else quote.market_cap,
                        change_5m=previous.change_5m if previous and quote.change_5m is None else quote.change_5m,
                        speed=previous.speed if previous and quote.speed is None else quote.speed,
                        cross_source_deviation_pct=deviation,
                        cross_source_verified=deviation is None or deviation <= 0.5,
                    )
                )
            with self._state_lock:
                for quote in verified:
                    current = self._latest_by_code.get(quote.code)
                    if current is None or _quote_version(quote) > _quote_version(current):
                        self._latest_by_code[quote.code] = quote
                return tuple(self._latest_by_code[code] for code in codes if code in self._latest_by_code)

    def health(self) -> Mapping[str, object]:
        now = self._monotonic()
        with self._state_lock:
            return {
                "active_source": self._latest_source,
                "cached_rows": len(self._latest_by_code),
                "route": _route_health(self._last_route_outcome),
                "sources": {
                    name: {
                        "planned_count": state.planned_count,
                        "success_count": state.success_count,
                        "error_count": state.error_count,
                        "consecutive_failures": state.failures,
                        "circuit_open": state.open_until > now,
                        "last_latency_ms": round(state.last_latency_ms, 2),
                        "p50_latency_ms": _percentile(state.latencies_ms, 0.50),
                        "p95_latency_ms": _percentile(state.latencies_ms, 0.95),
                        "last_error": state.last_error,
                    }
                    for name, state in self._states.items()
                },
            }

    def _record_planned(self, source: str) -> None:
        with self._state_lock:
            self._states[source].planned_count += 1

    def _is_open(self, source: str) -> bool:
        with self._state_lock:
            return self._states[source].open_until > self._monotonic()

    def _fetch_from_vendor(self, source: str, fetcher: Callable[[], Sequence[MarketQuote]]) -> Sequence[MarketQuote]:
        self._record_planned(source)
        if self._is_open(source):
            self._record_skipped_open(source)
            raise MarketDataFailed(source, "circuit_open")
        started = self._monotonic()
        try:
            quotes = tuple(fetcher())
        except MarketDataNoData as exc:
            self._record(source, False, started, str(exc))
            raise
        except Exception as exc:
            self._record(source, False, started, str(exc))
            raise MarketDataFailed(source, str(exc)) from exc
        if len(quotes) < self._minimum_market_rows:
            error = MarketDataNoData(f"{source}: only {len(quotes)} market rows")
            self._record(source, False, started, str(error))
            raise error
        self._record(source, True, started, "")
        return quotes

    def _record(self, source: str, success: bool, started: float, error: str) -> None:
        elapsed_ms = (self._monotonic() - started) * 1000.0
        with self._state_lock:
            state = self._states[source]
            state.last_latency_ms = elapsed_ms
            state.latencies_ms.append(elapsed_ms)
            if success:
                state.failures = 0
                state.success_count += 1
                state.last_error = ""
                state.open_until = 0.0
                return
            state.failures += 1
            state.error_count += 1
            state.last_error = error[:240]
            if state.failures >= self._failure_limit:
                state.open_until = self._monotonic() + self._breaker_seconds

    def _record_skipped_open(self, source: str) -> None:
        with self._state_lock:
            state = self._states[source]
            state.error_count += 1
            state.last_error = "circuit_open"


def _route_health(last_route: RouteOutcome | None) -> Mapping[str, object]:
    if not isinstance(last_route, RouteOutcome):
        return {
            "status": "idle",
            "used_vendor": None,
            "degraded": False,
            "fallback_reason": None,
            "attempted_count": 0,
            "success_count": 0,
            "failure_count": 0,
            "no_data_count": 0,
            "skipped_count": 0,
            "attempted_vendors": (),
        }
    attempted_vendors = [
        {
            "name": vendor.name,
            "status": vendor.status,
            "severity": vendor.severity.value,
            "error": vendor.error,
            "skipped": vendor.skipped,
            "duration_ms": round(vendor.duration_ms, 2) if vendor.duration_ms is not None else None,
        }
        for vendor in last_route.results
    ]
    return {
        "status": last_route.status,
        "used_vendor": last_route.vendor or None,
        "degraded": last_route.degraded,
        "fallback_reason": last_route.fallback_reason,
        "attempted_count": len(last_route.results),
        "success_count": sum(1 for vendor in last_route.results if vendor.status == "success"),
        "failure_count": sum(1 for vendor in last_route.results if vendor.status == "failed"),
        "no_data_count": sum(1 for vendor in last_route.results if vendor.status == "no_data"),
        "skipped_count": sum(1 for vendor in last_route.results if vendor.skipped),
        "attempted_vendors": attempted_vendors,
    }


def _route_outcome_from_exception(exc: Exception) -> RouteOutcome:
    route_outcome = getattr(exc, "route_outcome", None)
    if isinstance(route_outcome, RouteOutcome):
        return route_outcome
    if isinstance(exc, MarketDataNoData):
        return RouteOutcome(
            result=None,
            vendor="",
            results=(),
            degraded=True,
            status="no_data",
            fallback_reason="no_data",
        )
    if isinstance(exc, MarketDataFailed):
        vendor = str(getattr(exc, "vendor", "all_vendors"))
        return RouteOutcome(
            result=None,
            vendor=vendor,
            results=(),
            degraded=True,
            status="failed",
            fallback_reason="failed",
        )
    return RouteOutcome(result=None, vendor="", results=(), degraded=True, status="failed", fallback_reason="failed")


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * quantile + 0.5)))
    return round(float(ordered[index]), 2)


def _price_deviation_pct(first: float | None, second: float | None) -> float | None:
    if first is None or second is None or first <= 0 or second <= 0:
        return None
    return abs(first - second) / first * 100.0


def _quote_version(quote: MarketQuote) -> tuple[datetime, datetime, str]:
    return (quote.source_time, quote.received_time, quote.data_version)


__all__ = ["MarketDataGateway"]
