"""Scoped cancellation for explicit maintenance commands on the main thread."""

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Event

from trader.infra.shutdown import ShutdownDeadline, ShutdownSignalController, ShutdownStep, ShutdownWatchdogStartError


class ManualCancellationScope(Event):
    """One cancellation event and deadline shared with the command's resource cleanup."""

    def __init__(self, controller: ShutdownSignalController) -> None:
        super().__init__()
        self._controller = controller
        self._shutdown_started = False
        self._shutdown_reported = False
        self._shutdown_completed = True

    def shutdown_deadline(self) -> ShutdownDeadline:
        self._shutdown_started = True
        try:
            return self._controller.begin_shutdown()
        except ShutdownWatchdogStartError as exc:
            # The caller must still stop its resources under the established
            # budget, then report the capability failure through this scope.
            return exc.deadline

    @property
    def watchdog_failure(self) -> ShutdownWatchdogStartError | None:
        return self._controller.watchdog_failure

    @property
    def watchdog_started(self) -> bool:
        return self._controller.watchdog_started

    def record_shutdown(self, step: ShutdownStep) -> None:
        self._shutdown_reported = True
        self._shutdown_completed = self._shutdown_completed and step.completed

    @property
    def shutdown_completed(self) -> bool:
        return not self._shutdown_started or (self._shutdown_reported and self._shutdown_completed)


@contextmanager
def ManualCancellation(
    *, timeout_seconds: float, force_exit: Callable[[int], object] = os._exit
) -> Iterator[ManualCancellationScope]:
    """Keep cancellation and its watchdog active until all owned resources close."""
    def request_cancel(deadline: ShutdownDeadline) -> None:
        cancelled.set()
        _notice(f"正在取消，停止新任务并清理资源（最多{deadline.timeout_seconds:g}秒）；再次按 Ctrl+C 强制退出。\n")

    def stop(code: int) -> None:
        was_cancelled = bool(controller.exit_code or cancelled.is_set())
        if not controller.watchdog_started:
            _notice("资源关闭未完成且没有超时保护，正在强制退出。\n")
            force_exit(130 if was_cancelled else 2)
            return
        if not was_cancelled:
            _notice("资源关闭超时，正在强制退出。\n")
            force_exit(2)
            return
        _notice("取消清理超时，正在强制退出。\n" if code == 2 else "再次收到停止信号，正在强制退出。\n")
        force_exit(130)

    controller = ShutdownSignalController(
        timeout_seconds=timeout_seconds, on_first_signal=request_cancel, force_exit=stop
    )
    cancelled = ManualCancellationScope(controller)
    controller.install()
    try:
        yield cancelled
    finally:
        try:
            if cancelled.shutdown_completed:
                controller.mark_completed()
            elif not controller.watchdog_started:
                # There is no asynchronous guard left to bound interpreter
                # thread joins. An incomplete close must not return and hang.
                controller.force_shutdown()
        finally:
            controller.restore()


def _notice(message: str) -> None:
    # A signal may interrupt buffered progress output while it owns the stream lock.
    try:
        os.write(2, message.encode("utf-8"))
    except OSError:
        pass  # A closed stderr must not prevent cancellation or the hard-exit deadline.


__all__ = ["ManualCancellation", "ManualCancellationScope"]
