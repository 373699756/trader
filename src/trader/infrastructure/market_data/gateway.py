"""Market-data source fallback, single-flight and circuit-breaker owner."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from trader.domain.models import MarketQuote
from trader.infrastructure.market_data.eastmoney import EastmoneyClient
from trader.infrastructure.market_data.sina import SinaClient
from trader.infrastructure.market_data.tencent import TencentClient


@dataclass
class _CircuitState:
    failures: int = 0
    open_until: float = 0.0
    success_count: int = 0
    error_count: int = 0
    last_latency_ms: float = 0.0
    last_error: str = ""


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
        self._fetch_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._states = {"eastmoney": _CircuitState(), "sina": _CircuitState(), "tencent": _CircuitState()}
        self._latest_by_code: dict[str, MarketQuote] = {}
        self._latest_source = "unavailable"

    def fetch_market(self) -> Sequence[MarketQuote]:
        with self._fetch_lock:
            errors: list[str] = []
            for name, fetcher in (("eastmoney", self._eastmoney.fetch_market), ("sina", self._sina.fetch_market)):
                if self._is_open(name):
                    errors.append(f"{name}:circuit_open")
                    continue
                started = self._monotonic()
                try:
                    quotes = tuple(fetcher())
                    if len(quotes) < self._minimum_market_rows:
                        raise RuntimeError(f"only {len(quotes)} market rows")
                except Exception as exc:
                    self._record(name, False, started, str(exc))
                    errors.append(f"{name}:{exc}")
                    continue
                self._record(name, True, started, "")
                with self._state_lock:
                    self._latest_by_code = {quote.code: quote for quote in quotes}
                    self._latest_source = name
                return quotes
            with self._state_lock:
                cached = tuple(self._latest_by_code.values())
            if cached:
                return cached
            raise RuntimeError("market data unavailable: " + "; ".join(errors))

    def fetch_candidates(self, codes: Sequence[str]) -> Sequence[MarketQuote]:
        if not codes:
            return ()
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
        return tuple(verified)

    def health(self) -> Mapping[str, object]:
        now = self._monotonic()
        with self._state_lock:
            return {
                "active_source": self._latest_source,
                "cached_rows": len(self._latest_by_code),
                "sources": {
                    name: {
                        "success_count": state.success_count,
                        "error_count": state.error_count,
                        "consecutive_failures": state.failures,
                        "circuit_open": state.open_until > now,
                        "last_latency_ms": round(state.last_latency_ms, 2),
                        "last_error": state.last_error,
                    }
                    for name, state in self._states.items()
                },
            }

    def _is_open(self, source: str) -> bool:
        with self._state_lock:
            return self._states[source].open_until > self._monotonic()

    def _record(self, source: str, success: bool, started: float, error: str) -> None:
        elapsed_ms = (self._monotonic() - started) * 1000.0
        with self._state_lock:
            state = self._states[source]
            state.last_latency_ms = elapsed_ms
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


def _price_deviation_pct(first: float | None, second: float | None) -> float | None:
    if first is None or second is None or first <= 0 or second <= 0:
        return None
    return abs(first - second) / first * 100.0


__all__ = ["MarketDataGateway"]
