"""Scoped cancellation for explicit maintenance commands on the main thread."""

import signal
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Event


@contextmanager
def ManualCancellation() -> Iterator[Event]:
    """Stop dispatch on the first signal and restore handlers before the next phase."""
    cancelled = Event()

    def request_cancel(_signal: int, _frame: object) -> None:
        if cancelled.is_set():
            raise KeyboardInterrupt
        cancelled.set()

    previous = {signum: signal.signal(signum, request_cancel) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield cancelled
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


__all__ = ["ManualCancellation"]
