"""Bounded disposable processes for blocking SDKs without cooperative cancellation."""

from __future__ import annotations

import math
import multiprocessing
import os
import socket
import time
from collections.abc import Callable
from concurrent.futures import CancelledError
from contextlib import redirect_stderr, redirect_stdout
from typing import Protocol

from trader.infra.ipc_frames import ReceiveIpcFrame, SendIpcFrame

_MAXIMUM_RESULT_BYTES = 1024 * 1024


class IsolatedCall(Protocol):
    def __call__(self, operation: Callable[[], bytes], timeout_seconds: float) -> bytes: ...


def RunIsolatedCall(
    operation: Callable[[], bytes],
    timeout_seconds: float,
    *,
    cancel_requested: Callable[[], bool] | None = None,
) -> bytes:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("sdk_timeout_invalid")
    if cancel_requested is not None and cancel_requested():
        raise CancelledError("sdk_cancelled")
    context = multiprocessing.get_context("spawn")
    receiver, sender = socket.socketpair()
    process = context.Process(target=_invoke, args=(operation, sender), daemon=True)
    deadline = time.monotonic() + timeout_seconds
    try:
        process.start()
        sender.close()
        def check() -> float:
            if cancel_requested is not None and cancel_requested():
                raise CancelledError("sdk_cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("sdk_deadline_exceeded")
            return remaining
        try:
            result = ReceiveIpcFrame(receiver, maximum_bytes=_MAXIMUM_RESULT_BYTES + 1, check=check)
        except (TimeoutError, CancelledError):
            raise
        except (EOFError, OSError) as exc:
            raise RuntimeError("sdk_worker_exited") from exc
        if not result:
            raise RuntimeError("sdk_worker_response_invalid")
        if result[:1] == b"E":
            raise RuntimeError("sdk_failed_" + result[1:].decode("ascii"))
        if result[:1] != b"V":
            raise RuntimeError("sdk_worker_response_invalid")
        return result[1:]
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


def _invoke(operation: Callable[[], bytes], sender: socket.socket) -> None:
    try:
        with open(os.devnull, "w") as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
            value = operation()
        if not isinstance(value, bytes) or len(value) > _MAXIMUM_RESULT_BYTES:
            raise ValueError("sdk_result_invalid")
        SendIpcFrame(sender, b"V" + value)
    except BaseException as exc:
        try:
            SendIpcFrame(sender, b"E" + type(exc).__name__[:80].encode("ascii", errors="replace"))
        except (EOFError, OSError):
            return  # The supervising parent has already stopped or discarded this attempt.
    finally:
        sender.close()


__all__ = ["IsolatedCall", "RunIsolatedCall"]
