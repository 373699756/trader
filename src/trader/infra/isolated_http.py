"""Kill and reap a blocked HTTP attempt without retaining its socket or worker."""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import CancelledError
from dataclasses import dataclass, replace
from functools import partial

from trader.infra.http_contracts import HttpCancelled, HttpError, HttpRequest, HttpResponse, HttpTimeout, HttpTransport
from trader.infra.isolated_call import RunIsolatedCall


@dataclass(frozen=True)
class _HttpOutcome:
    response: HttpResponse | None
    failure: str = ""


def _execute(transport_factory: Callable[[], HttpTransport], request: HttpRequest) -> _HttpOutcome:
    try:
        with transport_factory() as transport:
            return _HttpOutcome(transport.execute(request))
    except HttpTimeout:
        return _HttpOutcome(None, "timeout")
    except HttpError:
        return _HttpOutcome(None, "transport")


def ExecuteIsolatedHttp(request: HttpRequest, *, transport_factory: Callable[[], HttpTransport]) -> HttpResponse:
    remaining = request.timeout_seconds
    if request.deadline is not None:
        remaining = min(remaining, request.deadline - time.monotonic())
    if remaining <= 0:
        raise HttpTimeout("http_deadline_exceeded")
    # Cancellation closures stay in the parent; only immutable request data crosses.
    operation = partial(_execute, transport_factory, replace(request, cancel_requested=None))
    try:
        outcome = RunIsolatedCall(operation, remaining, cancel_requested=request.cancel_requested)
    except CancelledError as exc:
        raise HttpCancelled("http_cancelled") from exc
    except TimeoutError as exc:
        raise HttpTimeout("http_deadline_exceeded") from exc
    except RuntimeError as exc:
        raise HttpError("http_worker_failed") from exc
    if request.cancel_requested is not None and request.cancel_requested():
        raise HttpCancelled("http_cancelled")
    if request.deadline is not None and time.monotonic() >= request.deadline:
        raise HttpTimeout("http_deadline_exceeded")
    if outcome.failure == "timeout":
        raise HttpTimeout("http_timeout")
    if outcome.response is None:
        raise HttpError("http_transport_failed")
    return outcome.response


__all__ = ["ExecuteIsolatedHttp"]
