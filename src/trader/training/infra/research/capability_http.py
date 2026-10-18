"""Bounded supplier transport; the composition root owns the injected HTTP session."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping

from trader.infra.http_contracts import HttpParameter, HttpResponse, HttpTimeout, HttpTransport


class BoundedCapabilitySession:
    MAX_REQUESTS = 2
    MAX_RESPONSE_BYTES = 4 * 1024 * 1024

    def __init__(self, session: HttpTransport, *, timeout_seconds: float, monotonic: Callable[[], float]) -> None:
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60:
            raise ValueError("supplier timeout must be finite and in (0, 60]")
        self._session = session
        self._monotonic = monotonic
        self._timeout_seconds = timeout_seconds
        self._deadline: float | None = None
        self._requests = 0

    def get(self, url: str, *, params: Mapping[str, HttpParameter], timeout: float) -> HttpResponse:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("request timeout must be finite and positive")
        if self._deadline is None:
            self._deadline = self._monotonic() + self.MAX_REQUESTS * self._timeout_seconds
        remaining = self._deadline - self._monotonic()
        if self._requests >= self.MAX_REQUESTS or remaining <= 0:
            raise HttpTimeout("capability request budget exhausted")
        self._requests += 1
        referer = "https://quote.eastmoney.com/" if "eastmoney" in url else "https://gu.qq.com/"
        response = self._session.get(
            url,
            params=params,
            timeout=min(timeout, self._timeout_seconds, remaining),
            headers={"User-Agent": "Mozilla/5.0", "Referer": referer},
            deadline=self._deadline,
            max_response_bytes=self.MAX_RESPONSE_BYTES,
        )
        response.raise_for_status()
        return response


__all__ = ["BoundedCapabilitySession"]
