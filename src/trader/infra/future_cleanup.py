"""Retain resource cleanup until every owned future has actually finished."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from threading import Lock

_LOGGER = logging.getLogger(__name__)


def CloseAfterFutures(close: Callable[[], None], pending: Sequence[Future[object]]) -> Future[object]:
    """Run cleanup once after pending work, without a second waiting thread."""
    completion: Future[object] = Future()
    completion.set_running_or_notify_cancel()
    remaining = len(pending)
    started = False
    lock = Lock()

    def finish(_future: Future[object] | None = None) -> None:
        nonlocal remaining, started
        with lock:
            if _future is not None:
                remaining -= 1
            if remaining or started:
                return
            started = True
        try:
            close()
        except BaseException as exc:
            completion.set_exception(exc)
            _LOGGER.error("deferred_resource_cleanup_failed error_type=%s", type(exc).__name__)
        else:
            completion.set_result(None)

    for future in pending:
        future.add_done_callback(finish)
    if not pending:
        finish()
    return completion


__all__ = ["CloseAfterFutures"]
