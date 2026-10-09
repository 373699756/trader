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
    RESOURCE_REJECTED = "resource_rejected"
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
        AdapterFailureCode.RESOURCE_REJECTED,
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
    code = _typed_failure_code(error, message, class_name)
    return AdapterFailure(
        code=code,
        provider=provider.strip().lower()[:80],
        operation=operation.strip().lower()[:120],
        retryable=code in _RETRYABLE,
        detail=code.value,
    )


def _typed_failure_code(error: BaseException, message: str, class_name: str) -> AdapterFailureCode:
    if isinstance(error, CancelledError) or "cancelled" in class_name or "canceled" in class_name:
        return AdapterFailureCode.CANCELLED
    if "superseded" in class_name or "superseded" in message:
        return AdapterFailureCode.SUPERSEDED
    if isinstance(error, TimeoutError) or "timeout" in class_name or "timeout" in message or "timed out" in message:
        return AdapterFailureCode.TIMEOUT
    if "deadline" in message or message.strip() == "late" or message.rstrip().endswith(": late"):
        return AdapterFailureCode.DEADLINE
    return _message_failure_code(message, class_name)


def _message_failure_code(message: str, class_name: str) -> AdapterFailureCode:
    if "circuit_open" in message:
        code = AdapterFailureCode.CIRCUIT_OPEN
    elif "negative_cache" in message:
        code = AdapterFailureCode.NEGATIVE_CACHE
    elif "no_data" in message or "no usable" in message or "only 0" in message:
        code = AdapterFailureCode.NO_DATA
    elif "resource_rejected" in message or "queue rejected" in message:
        code = AdapterFailureCode.RESOURCE_REJECTED
    elif "http_429" in message or "rate limit" in message:
        code = AdapterFailureCode.RATE_LIMITED
    elif "schema" in class_name or "schema" in message:
        code = AdapterFailureCode.SCHEMA_INVALID
    else:
        code = AdapterFailureCode.SOURCE_FAILED
    return code


__all__ = ["AdapterFailure", "AdapterFailureCode", "classify_adapter_failure"]
