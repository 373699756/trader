"""Shared structured failure taxonomy for external infrastructure adapters."""

from __future__ import annotations

from concurrent.futures import CancelledError
from dataclasses import dataclass
from enum import Enum


class AdapterFailureCode(str, Enum):
    TIMEOUT = "timeout"
    DEADLINE = "deadline"
    CIRCUIT_OPEN = "circuit_open"
    NEGATIVE_CACHE = "negative_cache"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    NO_DATA = "no_data"
    RATE_LIMITED = "rate_limited"
    SCHEMA_INVALID = "schema_invalid"
    SOURCE_FAILED = "source_failed"


_RETRYABLE = frozenset(
    {
        AdapterFailureCode.TIMEOUT,
        AdapterFailureCode.DEADLINE,
        AdapterFailureCode.CIRCUIT_OPEN,
        AdapterFailureCode.NEGATIVE_CACHE,
        AdapterFailureCode.SUPERSEDED,
        AdapterFailureCode.NO_DATA,
        AdapterFailureCode.RATE_LIMITED,
        AdapterFailureCode.SOURCE_FAILED,
    }
)


@dataclass(frozen=True)
class AdapterFailure:
    code: AdapterFailureCode
    provider: str
    operation: str
    retryable: bool
    detail: str


def classify_adapter_failure(
    error: BaseException,
    *,
    provider: str,
    operation: str,
) -> AdapterFailure:
    """Return a bounded, secret-free category instead of persisting exception text."""

    message = str(error).lower()
    class_name = error.__class__.__name__.lower()
    if isinstance(error, CancelledError) or "cancelled" in class_name or "canceled" in class_name:
        code = AdapterFailureCode.CANCELLED
    elif "superseded" in class_name or "superseded" in message:
        code = AdapterFailureCode.SUPERSEDED
    elif isinstance(error, TimeoutError) or "timeout" in class_name or "timeout" in message or "timed out" in message:
        code = AdapterFailureCode.TIMEOUT
    elif "deadline" in message or message.strip() == "late" or message.rstrip().endswith(": late"):
        code = AdapterFailureCode.DEADLINE
    elif "circuit_open" in message:
        code = AdapterFailureCode.CIRCUIT_OPEN
    elif "negative_cache" in message:
        code = AdapterFailureCode.NEGATIVE_CACHE
    elif "no_data" in message or "no usable" in message or "only 0" in message:
        code = AdapterFailureCode.NO_DATA
    elif "http_429" in message or "rate limit" in message:
        code = AdapterFailureCode.RATE_LIMITED
    elif "schema" in class_name or "schema" in message:
        code = AdapterFailureCode.SCHEMA_INVALID
    else:
        code = AdapterFailureCode.SOURCE_FAILED
    return AdapterFailure(
        code=code,
        provider=provider.strip().lower()[:80],
        operation=operation.strip().lower()[:120],
        retryable=code in _RETRYABLE,
        detail=code.value,
    )


__all__ = ["AdapterFailure", "AdapterFailureCode", "classify_adapter_failure"]
