"""Market-data source fallback, single-flight and circuit-breaker owner."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Generic, TypeVar, cast

from trader.application.ports import MarketDataUnavailable
from trader.domain.models import MarketQuote
from trader.infrastructure.market_data.eastmoney import EastmoneyClient
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

    def fetch_market(self) -> Sequence[MarketQuote]:
        return self._market_flight.run(self._fetch_market_once)

    def _fetch_market_once(self) -> Sequence[MarketQuote]:
        errors: list[str] = []
        last_exception: Exception | None = None
        for name, fetcher in (("eastmoney", self._eastmoney.fetch_market), ("sina", self._sina.fetch_market)):
            self._record_planned(name)
            if self._is_open(name):
                errors.append(f"{name}:circuit_open")
                continue
            started = self._monotonic()
            try:
                quotes = tuple(fetcher())
                if len(quotes) < self._minimum_market_rows:
                    raise RuntimeError(f"only {len(quotes)} market rows")
            except Exception as exc:
                last_exception = exc
                self._record(name, False, started, str(exc))
                errors.append(f"{name}:{exc}")
                continue
            self._record(name, True, started, "")
            with self._state_lock:
                for quote in quotes:
                    current = self._latest_by_code.get(quote.code)
                    if current is None or _quote_version(quote) > _quote_version(current):
                        self._latest_by_code[quote.code] = quote
                self._latest_source = name
                return tuple(self._latest_by_code.values())
        with self._state_lock:
            cached = tuple(self._latest_by_code.values())
        if cached:
            return cached
        unavailable = MarketDataUnavailable("market data unavailable: " + "; ".join(errors))
        if last_exception is not None:
            raise unavailable from last_exception
        raise unavailable

    def fetch_candidates(self, codes: Sequence[str]) -> Sequence[MarketQuote]:
        if not codes:
            return ()
        with self._candidate_fetch_lock:
            self._record_planned("tencent")
            if self._is_open("tencent"):
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
