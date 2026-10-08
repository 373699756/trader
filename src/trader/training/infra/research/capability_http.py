"""Bounded supplier transport; the composition root owns the injected HTTP session."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass

import requests


@dataclass(frozen=True)
class CapabilitySourceResponse:
    content: bytes

    def json(self) -> object:
        return json.loads(self.content)

    def raise_for_status(self) -> None:
        return None


class BoundedCapabilitySession:
    MAX_REQUESTS = 2
    MAX_RESPONSE_BYTES = 4 * 1024 * 1024

    def __init__(self, session: requests.Session, *, timeout_seconds: float, monotonic: Callable[[], float]) -> None:
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60:
            raise ValueError("supplier timeout must be finite and in (0, 60]")
        self._session = session
        self._monotonic = monotonic
        self._timeout_seconds = timeout_seconds
        self._deadline: float | None = None
        self._requests = 0

    def get(self, url: str, *, params: dict[str, object], timeout: float) -> CapabilitySourceResponse:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("request timeout must be finite and positive")
        if self._deadline is None:
            self._deadline = self._monotonic() + self.MAX_REQUESTS * self._timeout_seconds
        remaining = self._deadline - self._monotonic()
        if self._requests >= self.MAX_REQUESTS or remaining <= 0:
            raise requests.Timeout("capability request budget exhausted")
        self._requests += 1
        referer = "https://quote.eastmoney.com/" if "eastmoney" in url else "https://gu.qq.com/"
        with self._session.get(
            url,
            params=request_params(params),
            timeout=min(timeout, self._timeout_seconds, remaining),
            headers={"Referer": referer},
            stream=True,
            allow_redirects=False,
        ) as response:
            response.raise_for_status()
            if 300 <= response.status_code < 400:
                raise requests.RequestException("capability redirects are not allowed")
            body = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                if self._monotonic() >= self._deadline:
                    raise requests.Timeout("capability response deadline exceeded")
                if len(body) + len(chunk) > self.MAX_RESPONSE_BYTES:
                    raise ValueError("capability response exceeds byte budget")
                body.extend(chunk)
            if self._monotonic() >= self._deadline:
                raise requests.Timeout("capability response deadline exceeded")
            return CapabilitySourceResponse(bytes(body))


def request_params(params: dict[str, object]) -> dict[str, str | tuple[str, ...]]:
    normalized: dict[str, str | tuple[str, ...]] = {}
    for key, value in params.items():
        if isinstance(value, str):
            normalized[key] = value
        elif isinstance(value, tuple) and all(isinstance(item, str) for item in value):
            normalized[key] = value
        else:
            raise TypeError(f"unsupported H1 capability request parameter: {key}")
    return normalized
