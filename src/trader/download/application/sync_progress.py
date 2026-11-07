"""One command clock and independent task lifetimes for download feedback."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from threading import Lock
from typing import TypeVar

from trader.infra.progress_heartbeat import ProgressHeartbeat

from trader.download.application.progress_message import (
    ProgressDuration, ProgressField, ProgressMessage, ProgressTask, WithProgressTime,
)

_Result = TypeVar("_Result")


@dataclass
class _TaskTiming:
    started: float
    updated: float
    emitted: float
    latest: ProgressMessage
    ended: bool = False


class SyncProgress:
    """Project typed task events; heartbeats only repeat observed facts."""

    def __init__(
        self,
        report: Callable[[ProgressMessage], None],
        monotonic: Callable[[], float],
        *,
        started_at: float | None = None,
    ) -> None:
        now = monotonic()
        if started_at is not None and (not math.isfinite(started_at) or not 0 <= started_at <= now):
            raise ValueError("sync_progress_start_invalid")
        self._started = now if started_at is None else started_at
        self._monotonic = monotonic
        self._report = report
        self._tasks: dict[ProgressTask, _TaskTiming] = {}
        self._lock = Lock()

    def __call__(self, message: ProgressMessage) -> None:
        task = message.task
        if task is None:
            raise ValueError("sync_progress_task_missing")
        with self._lock:
            now = self._monotonic()
            timing = self._tasks.get(task)
            if timing is None:
                timing = _TaskTiming(now, now, float("-inf"), message)
                self._tasks[task] = timing
            elif message.state == "开始":
                timing.ended = False
            immediate = message.state in {"开始", "完成", "失败", "重试", "取消"} or message.finished or message.stock_failure_code is not None
            changed = message.fields != timing.latest.fields
            timing.latest = message
            if changed:
                timing.updated = now
            timing.ended = timing.ended or message.finished
            interval = 5.0 if message.state == "等待" else 1.0
            if immediate or now - timing.emitted >= interval:
                self._emit(task, timing, message, now)

    def _emit(self, task: ProgressTask, timing: _TaskTiming, message: ProgressMessage, now: float) -> None:
        self._report(WithProgressTime(message, now - self._started, now - timing.started))
        timing.emitted = now

    def heartbeat(self) -> None:
        """Expose each unfinished phase independently, even when other phases print."""
        with self._lock:
            now = self._monotonic()
            for task, timing in self._tasks.items():
                if not timing.ended and now - timing.emitted >= 5.0:
                    message = replace(timing.latest, state="等待", fields=(
                        *timing.latest.fields,
                        ProgressField(f"距该阶段事实更新 {ProgressDuration(now - timing.updated)}"),
                    ))
                    self._emit(task, timing, message, now)

    def finish(self, task: ProgressTask) -> None:
        """Close an explicitly scoped operation without inventing a success event."""
        with self._lock:
            timing = self._tasks.get(task)
            if timing is not None:
                timing.ended = True

    def finish_owner(self, owner: str) -> None:
        with self._lock:
            for task, timing in self._tasks.items():
                if task.owner == owner:
                    timing.ended = True

    def report_disk(self, text: str) -> None:
        self(ProgressMessage((ProgressField(text),), task=ProgressTask("启动", "disk", "存储检测")))

    def run(self, owner: str, operation: Callable[[], _Result]) -> _Result:
        try:
            return operation()
        finally:
            self.finish_owner(owner)

    def command(self, operation: Callable[[], _Result], *, monitor: bool) -> _Result:
        """Run the command under the explicitly owned observation scope."""
        with self.keep_alive(enabled=monitor):
            return operation()

    @contextmanager
    def keep_alive(self, *, enabled: bool = True) -> Iterator[None]:
        if not enabled:
            yield
            return
        with ProgressHeartbeat[bool](lambda tick: self.heartbeat(), interval_seconds=1.0) as timer:
            timer(True)
            try:
                yield
            finally:
                with self._lock:
                    self._tasks.clear()


__all__ = ["SyncProgress"]
