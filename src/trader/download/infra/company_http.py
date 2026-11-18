"""Shared, bounded company source HTTP error taxonomy."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from functools import wraps
from typing import ParamSpec, TypeVar

from trader.download.domain.company_sync import CompanySourceCancelled

from trader.download.domain.company_evidence import CompanySourceFailure
from trader.infra.http_contracts import HttpCancelled, HttpError, HttpResponse, HttpResponseTooLarge, HttpStatusError, HttpTimeout


_SourceParameters = ParamSpec("_SourceParameters")
_SourceResult = TypeVar("_SourceResult")


def CompanySourceOperation(operation: Callable[_SourceParameters, _SourceResult]) -> Callable[_SourceParameters, _SourceResult]:
    """Translate transport failures only; local file and persistence errors escape."""
    @wraps(operation)
    def run(*args: _SourceParameters.args, **kwargs: _SourceParameters.kwargs) -> _SourceResult:
        try:
            return operation(*args, **kwargs)
        except HttpCancelled as exc:
            raise CompanySourceCancelled(str(exc)) from exc
        except HttpTimeout as exc:
            raise CompanySourceFailure("timeout") from exc
        except HttpResponseTooLarge as exc:
            raise CompanySourceFailure("http_response_too_large") from exc
        except HttpError as exc:
            if str(exc) == "http_compressed_body_invalid":
                raise CompanySourceFailure("http_compressed_body_invalid") from exc
            if str(exc) == "http_content_encoding_unsupported":
                raise CompanySourceFailure("http_content_encoding_unsupported") from exc
            raise CompanySourceFailure("http_transport_error") from exc
    return run


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


__all__ = ["CheckCompanyResponse", "CompanyRequestPacer", "CompanySourceOperation"]
