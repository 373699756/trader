"""Shared, bounded company source HTTP error taxonomy."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock

from trader.download.domain.company_evidence import CompanySourceFailure
from trader.infra.http_contracts import HttpCancelled, HttpResponse, HttpStatusError


class CompanyRequestPacer:
    def __init__(self, interval: float, monotonic: Callable[[], float], sleeper: Callable[[float], None], cancelled: Callable[[], bool] | None) -> None:
        self._interval, self._monotonic, self._sleep, self._cancelled = interval, monotonic, sleeper, cancelled
        self._next = 0.0
        self._lock = Lock()

    def wait(self) -> None:
        while not self._lock.acquire(timeout=0.1):
            self._check()
        try:
            self._check()
            while (delay := self._next - self._monotonic()) > 0:
                self._sleep(min(0.1, delay))
                self._check()
            self._next = self._monotonic() + self._interval
        finally:
            self._lock.release()

    def _check(self) -> None:
        if self._cancelled is not None and self._cancelled():
            raise HttpCancelled("http_cancelled")


def CheckCompanyResponse(response: HttpResponse) -> None:
    try:
        response.raise_for_status()
    except HttpStatusError as exc:
        if response.status_code == 403:
            raise CompanySourceFailure("access_denied", status_code=exc.status_code) from exc
        if response.status_code == 429:
            raise CompanySourceFailure("rate_limited", status_code=exc.status_code) from exc
        raise CompanySourceFailure("http_error", status_code=exc.status_code) from exc


__all__ = ["CheckCompanyResponse", "CompanyRequestPacer"]
