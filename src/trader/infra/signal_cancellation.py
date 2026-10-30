"""Scoped cancellation for explicit maintenance commands on the main thread."""

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Event

from trader.infra.shutdown import ShutdownDeadline, ShutdownSignalController


@contextmanager
def ManualCancellation(
    *, timeout_seconds: float, force_exit: Callable[[int], object] = os._exit
) -> Iterator[Event]:
    """Keep cancellation and its watchdog active until all owned resources close."""
    cancelled = Event()

    def request_cancel(deadline: ShutdownDeadline) -> None:
        cancelled.set()
        _notice(f"正在取消，停止新任务并清理资源（最多{deadline.timeout_seconds:g}秒）；再次按 Ctrl+C 强制退出。\n")

    def stop(code: int) -> None:
        _notice("取消清理超时，正在强制退出。\n" if code == 2 else "再次收到停止信号，正在强制退出。\n")
        force_exit(130)

    controller = ShutdownSignalController(
        timeout_seconds=timeout_seconds, on_first_signal=request_cancel, force_exit=stop
    )
    controller.install()
    try:
        yield cancelled
    finally:
        controller.mark_completed()
        controller.restore()


def _notice(message: str) -> None:
    # A signal may interrupt buffered progress output while it owns the stream lock.
    try:
        os.write(2, message.encode("utf-8"))
    except OSError:
        pass  # A closed stderr must not prevent cancellation or the hard-exit deadline.


__all__ = ["ManualCancellation"]
