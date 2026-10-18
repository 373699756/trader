"""Bounded disposable processes for blocking SDKs without cooperative cancellation."""

from __future__ import annotations

import math
import multiprocessing
import os
import time
from collections.abc import Callable
from concurrent.futures import CancelledError
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from multiprocessing.connection import Connection
from typing import Generic, Protocol, TypeVar, cast

_Value = TypeVar("_Value")


class IsolatedCall(Protocol):
    def __call__(self, operation: Callable[[], _Value], timeout_seconds: float) -> _Value: ...


@dataclass(frozen=True)
class _Outcome(Generic[_Value]):
    value: _Value | None
    failure: str


def RunIsolatedCall(
    operation: Callable[[], _Value],
    timeout_seconds: float,
    *,
    cancel_requested: Callable[[], bool] | None = None,
) -> _Value:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("sdk_timeout_invalid")
    if cancel_requested is not None and cancel_requested():
        raise CancelledError("sdk_cancelled")
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_invoke, args=(operation, sender), daemon=True)
    deadline = time.monotonic() + timeout_seconds
    try:
        process.start()
        sender.close()
        while True:
            if cancel_requested is not None and cancel_requested():
                raise CancelledError("sdk_cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("sdk_deadline_exceeded")
            if receiver.poll(min(0.05, remaining)):
                try:
                    result = cast(_Outcome[_Value], receiver.recv())
                except EOFError as exc:
                    raise RuntimeError("sdk_worker_exited") from exc
                if result.failure:
                    raise RuntimeError(f"sdk_failed_{result.failure}")
                return cast(_Value, result.value)
            if not process.is_alive():
                raise RuntimeError("sdk_worker_exited")
    finally:
        sender.close()
        receiver.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=1.0)
            if process.is_alive():
                process.kill()
                process.join(timeout=1.0)
            process.close()


def _invoke(operation: Callable[[], _Value], sender: Connection) -> None:
    try:
        with open(os.devnull, "w") as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
            value = operation()
        sender.send(_Outcome(value, ""))
    except BaseException as exc:
        sender.send(_Outcome(None, type(exc).__name__))
    finally:
        sender.close()


__all__ = ["IsolatedCall", "RunIsolatedCall"]
