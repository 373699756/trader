"""Bounded fan-out of one aligned history stream to independent training jobs."""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterator
from concurrent.futures import Executor, Future, wait
from contextlib import AbstractContextManager, closing, nullcontext
from dataclasses import dataclass, field
from datetime import date
from itertools import islice
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event
from typing import Generic, TypeVar

from trader.download.domain.history_revision import HistoryTrainingRow
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.application.resources import TrainingExecutionObserver, TrainingExecutionTiming

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
    timing: TrainingExecutionTiming | None = field(default=None, kw_only=True)


def RunSelectedTrainingTasks(
    tasks: tuple[SelectedTrainingTask[ResultT], ...],
    read_rows: Callable[[frozenset[date]], AbstractContextManager[Iterator[HistoryTrainingRow]]],
    executor: Executor,
    cancelled: Callable[[], bool],
    *,
    on_outcome: Callable[[SelectedTrainingOutcome[ResultT]], None] | None = None,
    observer: TrainingExecutionObserver | None = None,
) -> SelectedTrainingRun[ResultT]:
    if not tasks or len({task.profile for task in tasks}) != len(tasks):
        raise ValueError("training_requires_unique_selected_profiles")
    return _TrainingFanout(
        tasks,
        executor,
        cancelled,
        on_outcome,
        observer,
    ).execute(read_rows)


class _TrainingFanout(Generic[ResultT]):
    def __init__(
        self,
        tasks: tuple[SelectedTrainingTask[ResultT], ...],
        executor: Executor,
        cancelled: Callable[[], bool],
        on_outcome: Callable[[SelectedTrainingOutcome[ResultT]], None] | None,
        observer: TrainingExecutionObserver | None,
    ) -> None:
        self.tasks = tasks
        self.executor = executor
        self.cancelled = cancelled
        self.on_outcome = on_outcome
        self.observer = observer
        self.queues: tuple[Queue[tuple[HistoryTrainingRow, ...] | None], ...] = tuple(Queue(maxsize=2) for _ in tasks)
        self.stopped = Event()
        self.futures: list[Future[SelectedTrainingOutcome[ResultT]]] = []

    def _receive(self, index: int) -> Generator[HistoryTrainingRow, None, None]:
        queue = self.queues[index]
        while True:
            if self.cancelled() or self.stopped.is_set():
                raise InterruptedError("selected_training_cancelled")
            try:
                with self.observer.receive_queue(self.tasks[index].profile) if self.observer is not None else nullcontext():
                    batch = queue.get(timeout=0.1)
            except Empty:
                continue
            if batch is None:
                return
            yield from batch

    def _run(self, index: int) -> SelectedTrainingOutcome[ResultT]:
        task = self.tasks[index]
        try:
            with self.observer.task(task.profile) if self.observer is not None else nullcontext():
                if self.cancelled():
                    raise InterruptedError("selected_training_cancelled")
                with closing(self._receive(index)) as rows:
                    outcome = SelectedTrainingOutcome(task.profile, task.execute(rows))
        except InterruptedError:
            outcome = SelectedTrainingOutcome(task.profile, None, "cancelled", True)
        except (OSError, RuntimeError, ValueError, ImportError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            outcome = SelectedTrainingOutcome(task.profile, None, reason or type(exc).__name__)
        if self.on_outcome is not None:
            self.on_outcome(outcome)
        return outcome

    def _send(self, index: int, batch: tuple[HistoryTrainingRow, ...] | None) -> None:
        with self.observer.send_queue(self.tasks[index].profile) if self.observer is not None else nullcontext():
            self._put(index, batch)

    def _put(self, index: int, batch: tuple[HistoryTrainingRow, ...] | None) -> None:
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
        while True:
            if self.cancelled():
                raise InterruptedError("selected_training_cancelled")
            if not any(
                task.readable_dates and not future.done()
                for task, future in zip(self.tasks, self.futures, strict=True)
            ):
                break
            with self.observer.read_source() if self.observer is not None else nullcontext():
                batch = list(islice(rows, 256))
            scanned += len(batch)
            if not batch:
                break
            self._dispatch(batch)
        for index in range(len(self.tasks)):
            self._send(index, None)
        return scanned

    def execute(
        self, read_rows: Callable[[frozenset[date]], AbstractContextManager[Iterator[HistoryTrainingRow]]]
    ) -> SelectedTrainingRun[ResultT]:
        dates = frozenset(day for task in self.tasks for day in task.readable_dates)
        try:
            if self.cancelled():
                raise InterruptedError("selected_training_cancelled")
            for index in range(len(self.tasks)):
                self.futures.append(self.executor.submit(self._run, index))
            with read_rows(dates) if dates else nullcontext(iter(())) as rows:
                scanned = self._stream(rows)
            wait(self.futures)
            outcomes = tuple(future.result() for future in self.futures)
            return SelectedTrainingRun(outcomes, int(bool(dates)), scanned)
        except BaseException:
            self.stopped.set()
            wait(self.futures)
            raise
        finally:
            # Failed consumers can leave bounded batches behind, including when
            # an exception traceback retains this fan-out until later reporting.
            for queue in self.queues:
                while True:
                    try:
                        queue.get_nowait()
                    except Empty:
                        break
