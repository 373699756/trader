"""Bounded fan-out of one aligned history stream to independent training jobs."""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterator
from concurrent.futures import Executor, Future, wait
from contextlib import AbstractContextManager, closing, nullcontext
from dataclasses import dataclass, field, replace
from datetime import date
from itertools import islice
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event
from typing import Generic, Protocol, TypeVar

from trader.download.domain.history_revision import HistoryTrainingRow
from trader.download.domain.current_industry import CurrentIndustrySnapshot
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.application.learned_rank_training import V1TrainingResult
from trader.training.application.profile_training import TrainingRunResult
from trader.training.application.resources import TrainingExecutionObserver, TrainingExecutionTiming, TrainingFanoutSettings
from trader.training.application.tomorrow_training import TomorrowTrainingProgressPort
from trader.training.application.training_diagnostics import BuildTrainingOperationIssue, TrainingOperationIssue

ResultT = TypeVar("ResultT")
SelectedProfileResult = TrainingRunResult | V1TrainingResult


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
    delivery_issues: tuple[TrainingOperationIssue, ...] = ()


@dataclass(frozen=True)
class SelectedTrainingRun(Generic[ResultT]):
    outcomes: tuple[SelectedTrainingOutcome[ResultT], ...]
    history_scan_passes: int
    history_rows_read: int
    timing: TrainingExecutionTiming | None = field(default=None, kw_only=True)
    industry_snapshot: CurrentIndustrySnapshot | None = field(default=None, kw_only=True)


class SelectedTrainingPreparationError(RuntimeError):
    """One profile's input boundary failed without stopping other selected profiles."""


class SelectedTrainingSessionPort(Protocol[ResultT]):
    def prepare_task(
        self, profile: ScoringProfileId, progress: TomorrowTrainingProgressPort,
    ) -> SelectedTrainingTask[ResultT]: ...

    def read_rows(self, dates: frozenset[date]) -> AbstractContextManager[Iterator[HistoryTrainingRow]]: ...

    def native_runtime(
        self, tasks: tuple[SelectedTrainingTask[ResultT], ...],
    ) -> AbstractContextManager[tuple[SelectedTrainingTask[ResultT], ...]]: ...

    def completed(self, run: SelectedTrainingRun[ResultT]) -> SelectedTrainingRun[ResultT]: ...


class SelectedTrainingPort(Protocol[ResultT]):
    @property
    def profiles(self) -> tuple[ScoringProfileId, ...]: ...

    @property
    def progresses(self) -> tuple[TomorrowTrainingProgressPort, ...]: ...

    @property
    def cancelled(self) -> Callable[[], bool]: ...

    @property
    def settings(self) -> TrainingFanoutSettings: ...

    @property
    def observer(self) -> TrainingExecutionObserver: ...

    @property
    def on_outcome(self) -> Callable[[SelectedTrainingOutcome[ResultT]], None] | None: ...

    def open(self) -> AbstractContextManager[SelectedTrainingSessionPort[ResultT]]: ...


def TrainSelectedProfiles(operations: SelectedTrainingPort[ResultT], executor: Executor) -> SelectedTrainingRun[ResultT]:
    """Prepare every selected profile and own the one-scan fan-out execution order."""
    if not operations.profiles or len(set(operations.profiles)) != len(operations.profiles):
        raise ValueError("training_requires_unique_selected_profiles")
    with operations.open() as session:
        tasks = []
        for profile, progress in zip(operations.profiles, operations.progresses, strict=True):
            try:
                task = session.prepare_task(profile, progress)
            except SelectedTrainingPreparationError as exc:
                reason = str(exc)

                def failed(_rows: Iterator[HistoryTrainingRow], reason: str = reason) -> ResultT:
                    raise ValueError(reason)

                task = SelectedTrainingTask(profile, frozenset(), failed)
            tasks.append(task)
        with session.native_runtime(tuple(tasks)) as prepared:
            run = RunSelectedTrainingTasks(
                prepared,
                session.read_rows,
                executor,
                operations.cancelled,
                settings=operations.settings,
                on_outcome=operations.on_outcome,
                observer=operations.observer,
            )
        return session.completed(run)


class _TrainingQueue(Queue[tuple[HistoryTrainingRow, ...] | None]):
    def __init__(
        self,
        batches: int,
        profile: ScoringProfileId,
        observer: TrainingExecutionObserver | None,
    ) -> None:
        super().__init__(maxsize=batches)
        self._profile = profile
        self._observer = observer

    def _put(self, item: tuple[HistoryTrainingRow, ...] | None) -> None:
        super()._put(item)
        # Queue calls this hook while holding its mutex, before a consumer can drain it.
        if item is not None and self._observer is not None:
            self._observer.queue_depth(self._profile, self._qsize())


def RunSelectedTrainingTasks(
    tasks: tuple[SelectedTrainingTask[ResultT], ...],
    read_rows: Callable[[frozenset[date]], AbstractContextManager[Iterator[HistoryTrainingRow]]],
    executor: Executor,
    cancelled: Callable[[], bool],
    *,
    settings: TrainingFanoutSettings,
    on_outcome: Callable[[SelectedTrainingOutcome[ResultT]], None] | None = None,
    observer: TrainingExecutionObserver | None = None,
) -> SelectedTrainingRun[ResultT]:
    if not tasks or len({task.profile for task in tasks}) != len(tasks):
        raise ValueError("training_requires_unique_selected_profiles")
    return _TrainingFanout(
        tasks,
        executor,
        cancelled,
        settings,
        on_outcome,
        observer,
    ).execute(read_rows)


class _TrainingFanout(Generic[ResultT]):
    def __init__(
        self,
        tasks: tuple[SelectedTrainingTask[ResultT], ...],
        executor: Executor,
        cancelled: Callable[[], bool],
        settings: TrainingFanoutSettings,
        on_outcome: Callable[[SelectedTrainingOutcome[ResultT]], None] | None,
        observer: TrainingExecutionObserver | None,
    ) -> None:
        self.tasks = tasks
        self.executor = executor
        self.cancelled = cancelled
        self.settings = settings
        self.on_outcome = on_outcome
        self.observer = observer
        self.queues: tuple[Queue[tuple[HistoryTrainingRow, ...] | None], ...] = tuple(
            _TrainingQueue(settings.queue_batches, task.profile, observer) for task in tasks
        )
        self.stopped = Event()
        self.futures: list[Future[SelectedTrainingOutcome[ResultT]]] = []

    def _receive(self, index: int) -> Generator[HistoryTrainingRow, None, None]:
        queue = self.queues[index]
        while True:
            if self.cancelled() or self.stopped.is_set():
                raise InterruptedError("selected_training_cancelled")
            try:
                with self.observer.receive_queue(self.tasks[index].profile) if self.observer is not None else nullcontext():
                    try:
                        batch = queue.get_nowait()
                    except Empty:
                        with self.observer.wait_queue(self.tasks[index].profile, "empty") if self.observer is not None else nullcontext():
                            batch = queue.get(timeout=0.1)
            except Empty:
                continue
            if batch is None:
                return
            if batch and self.observer is not None:
                self.observer.received_batch(self.tasks[index].profile)
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
            try:
                self.on_outcome(outcome)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                outcome = replace(
                    outcome, delivery_issues=(BuildTrainingOperationIssue("outcome_notification", exc),),
                )
        return outcome

    def _send(self, index: int, batch: tuple[HistoryTrainingRow, ...] | None) -> None:
        with self.observer.send_queue(self.tasks[index].profile) if self.observer is not None else nullcontext():
            self._put(index, batch)

    def _put(self, index: int, batch: tuple[HistoryTrainingRow, ...] | None) -> None:
        while not self.futures[index].done():
            if self.cancelled():
                raise InterruptedError("selected_training_cancelled")
            try:
                try:
                    self.queues[index].put_nowait(batch)
                except Full:
                    with self.observer.wait_queue(self.tasks[index].profile, "full") if self.observer is not None else nullcontext():
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
                batch = list(islice(rows, self.settings.batch_rows))
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


__all__ = [
    "RunSelectedTrainingTasks",
    "SelectedProfileResult",
    "SelectedTrainingOutcome",
    "SelectedTrainingPort",
    "SelectedTrainingPreparationError",
    "SelectedTrainingRun",
    "SelectedTrainingSessionPort",
    "SelectedTrainingTask",
    "TrainingLocations",
    "TrainSelectedProfiles",
]
