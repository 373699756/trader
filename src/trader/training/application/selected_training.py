"""Bounded fan-out of one aligned history stream to independent training jobs."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import Executor, Future, wait
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event
from typing import Generic, TypeVar

from trader.download.domain.history_revision import HistoryTrainingRow
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId

ResultT = TypeVar("ResultT")


@dataclass(frozen=True)
class TrainingLocations:
    history: Path
    models: Path
    samples: Path


@dataclass(frozen=True)
class SelectedTrainingTask(Generic[ResultT]):
    profile: ScoringProfileId
    readable_dates: frozenset[date]
    execute: Callable[[Iterator[HistoryTrainingRow]], ResultT]


@dataclass(frozen=True)
class SelectedTrainingOutcome(Generic[ResultT]):
    profile: ScoringProfileId
    result: ResultT | None
    failure: str | None = None
    cancelled: bool = False


@dataclass(frozen=True)
class SelectedTrainingRun(Generic[ResultT]):
    outcomes: tuple[SelectedTrainingOutcome[ResultT], ...]
    history_scan_passes: int
    history_rows_read: int


def RunSelectedTrainingTasks(
    tasks: tuple[SelectedTrainingTask[ResultT], ...],
    read_rows: Callable[[frozenset[date]], Iterator[HistoryTrainingRow]],
    executor: Executor,
    cancelled: Callable[[], bool],
) -> SelectedTrainingRun[ResultT]:
    if not tasks or len({task.profile for task in tasks}) != len(tasks):
        raise ValueError("training_requires_unique_selected_profiles")
    return _TrainingFanout(tasks, executor, cancelled).execute(read_rows)


class _TrainingFanout(Generic[ResultT]):
    def __init__(
        self, tasks: tuple[SelectedTrainingTask[ResultT], ...], executor: Executor, cancelled: Callable[[], bool]
    ) -> None:
        self.tasks = tasks
        self.executor = executor
        self.cancelled = cancelled
        self.queues: tuple[Queue[tuple[HistoryTrainingRow, ...] | None], ...] = tuple(Queue(maxsize=2) for _ in tasks)
        self.stopped = Event()
        self.futures: list[Future[SelectedTrainingOutcome[ResultT]]] = []

    def _receive(self, queue: Queue[tuple[HistoryTrainingRow, ...] | None]) -> Iterator[HistoryTrainingRow]:
        while True:
            if self.cancelled() or self.stopped.is_set():
                raise InterruptedError("selected_training_cancelled")
            try:
                batch = queue.get(timeout=0.1)
            except Empty:
                continue
            if batch is None:
                return
            yield from batch

    def _run(self, index: int) -> SelectedTrainingOutcome[ResultT]:
        task = self.tasks[index]
        try:
            if self.cancelled():
                raise InterruptedError("selected_training_cancelled")
            return SelectedTrainingOutcome(task.profile, task.execute(self._receive(self.queues[index])))
        except InterruptedError:
            return SelectedTrainingOutcome(task.profile, None, "cancelled", True)
        except (OSError, RuntimeError, ValueError, ImportError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            return SelectedTrainingOutcome(task.profile, None, reason or type(exc).__name__)

    def _send(self, index: int, batch: tuple[HistoryTrainingRow, ...] | None) -> None:
        while not self.futures[index].done():
            if self.cancelled():
                raise InterruptedError("selected_training_cancelled")
            try:
                self.queues[index].put(batch, timeout=0.1)
                return
            except Full:
                continue

    def _dispatch(self, batch: list[HistoryTrainingRow]) -> None:
        for index, task in enumerate(self.tasks):
            selected = tuple(item for item in batch if item.trade_date in task.readable_dates)
            if selected:
                self._send(index, selected)

    def _stream(self, rows: Iterator[HistoryTrainingRow]) -> int:
        scanned = 0
        batch: list[HistoryTrainingRow] = []
        for row in rows:
            scanned += 1
            batch.append(row)
            if len(batch) == 256:
                if self.cancelled():
                    raise InterruptedError("selected_training_cancelled")
                self._dispatch(batch)
                batch.clear()
        self._dispatch(batch)
        for index in range(len(self.tasks)):
            self._send(index, None)
        return scanned

    def execute(
        self, read_rows: Callable[[frozenset[date]], Iterator[HistoryTrainingRow]]
    ) -> SelectedTrainingRun[ResultT]:
        dates = frozenset(day for task in self.tasks for day in task.readable_dates)
        try:
            for index in range(len(self.tasks)):
                self.futures.append(self.executor.submit(self._run, index))
            scanned = self._stream(read_rows(dates) if dates else iter(()))
            wait(self.futures)
            outcomes = tuple(future.result() for future in self.futures)
            return SelectedTrainingRun(outcomes, int(bool(dates)), scanned)
        except BaseException:
            self.stopped.set()
            wait(self.futures)
            raise
