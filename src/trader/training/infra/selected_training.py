"""Pinned history and per-profile adapters for the shared streaming use case."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy
from dataclasses import field

import logging
import time
from collections.abc import Callable, Generator, Iterator
from concurrent.futures import Executor
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, replace
from datetime import date
from functools import partial
from threadpoolctl import ThreadpoolController

from trader.download.domain.history_revision import HistoryTrainingRow
from trader.download.infra.history_control_index import HistoryMaintenanceLock
from trader.download.infra.history_partition_repack import RequireHistoryRepackIdle
from trader.infra.settings import RuntimeSettings
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.application.resources import TrainingFanoutSettings, TrainingMemoryAdmission, TrainingSampleSettings
from trader.training.application.selected_training import (
    RunSelectedTrainingTasks,
    SelectedTrainingOutcome,
    SelectedTrainingRun,
    SelectedTrainingTask,
    TrainingLocations,
)
from trader.training.application.tomorrow_training import TomorrowTrainingProgressPort
from trader.training.domain.v1.learned_rank import BuildLearnedRankDateSplit
from trader.training.infra.engine import (
    FitSelectedProfile,
    InspectSelectedProfile,
    ProfileTrainingRequest,
    TrainingRunResult,
)
from trader.training.infra.execution_timing import TrainingExecutionMeasurements
from trader.training.infra.history.aligned_training_rows import AlignmentReadDates, AlignTrainingRows
from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot
from trader.training.infra.profile.v1.settings import LoadV1TrainingSettings
from trader.training.infra.profile.v1.training import InspectV1Training, RunV1Training, V1TrainingResult
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.profile.v5.settings import TcnFittingParameters
from trader.training.infra.training_settings import LoadProfileTrainingSettings
from trader.training.infra.cpu_admission import TrainingCpuBudget
from trader.training.infra.execution_options import TrainingExecutionOptions

SelectedProfileResult = TrainingRunResult | V1TrainingResult


def _run_v1(execute: Callable[..., V1TrainingResult], rows: Iterator[HistoryTrainingRow]) -> V1TrainingResult:
    return execute(aligned_rows=rows)


def _reused(result: SelectedProfileResult, _rows: Iterator[HistoryTrainingRow]) -> SelectedProfileResult:
    return result


def _failed(reason: str, _rows: Iterator[HistoryTrainingRow]) -> SelectedProfileResult:
    raise ValueError(reason)


def _run_with_native_limits(
    execute: Callable[[Iterator[HistoryTrainingRow]], SelectedProfileResult],
    controller: ThreadpoolController,
    threads: int,
    rows: Iterator[HistoryTrainingRow],
) -> SelectedProfileResult:
    # OpenMP limits may be thread-local. All workers use the same baseline;
    # only the owning thread restores process-wide limits after the fan-out joins.
    controller.limit(limits=threads)
    return execute(rows)


@dataclass(frozen=True)
class SelectedTrainingRequest:
    runtime: RuntimeSettings
    profiles: tuple[ScoringProfileId, ...]
    progresses: tuple[TomorrowTrainingProgressPort, ...]
    cancelled: Callable[[], bool]
    memory_admission: TrainingMemoryAdmission
    paths: TrainingLocations
    cpu_budget: TrainingCpuBudget = field(kw_only=True)
    fanout_settings: TrainingFanoutSettings = field(kw_only=True)
    sample_settings: TrainingSampleSettings = field(kw_only=True)
    on_outcome: Callable[[SelectedTrainingOutcome[SelectedProfileResult]], None] | None = None
    measurements: TrainingExecutionMeasurements | None = field(default=None, kw_only=True)
    execution_options: TrainingExecutionOptions = field(default_factory=TrainingExecutionOptions, kw_only=True)
    memory_capacity_bytes: int | None = field(default=None, kw_only=True)


def TrainSelectedProfiles(
    request: SelectedTrainingRequest, executor: Executor, *, sqlite_policy: SqliteIoPolicy
) -> SelectedTrainingRun[SelectedProfileResult]:
    paths = request.paths
    measurements = (
        request.measurements
        if request.measurements is not None
        else TrainingExecutionMeasurements(request.profiles, time.monotonic)
    )
    with ExitStack() as scope:
        scope.enter_context(HistoryMaintenanceLock(paths.history.with_name(".history.lock")))
        for profile in request.profiles:
            scope.enter_context(HistoryMaintenanceLock(paths.models / f".{profile}.training.lock"))
        RequireHistoryRepackIdle(paths.history)
        history = SQLiteHistoryTrainingInputSnapshot.open(paths.history, sqlite_policy=sqlite_policy)
        factory = _TrainingTaskFactory(
            request,
            history,
            scope,
            measurements,
            sqlite_policy=sqlite_policy,
        )
        tasks = tuple(
            factory.prepare(profile, progress)
            for profile, progress in zip(request.profiles, request.progresses, strict=True)
        )
        physical_rows = 0

        def source_rows(dates: frozenset[date]) -> Generator[HistoryTrainingRow, None, None]:
            nonlocal physical_rows
            with closing(history.iter_training_rows(dates)) as rows:
                for row in rows:
                    physical_rows += 1
                    yield row

        @contextmanager
        def read_rows(dates: frozenset[date]) -> Iterator[Iterator[HistoryTrainingRow]]:
            readable = AlignmentReadDates(history.snapshot.calendar.open_dates, dates)
            with closing(source_rows(readable)) as source, closing(AlignTrainingRows(
                source, tuple(sorted(readable)), history.snapshot.listing_intervals
            )) as rows:
                yield rows

        with request.cpu_budget.native_runtime() as controller:
            tasks = tuple(replace(task, execute=partial(
                _run_with_native_limits, task.execute, controller, request.cpu_budget.native_threads
            )) for task in tasks)
            run = RunSelectedTrainingTasks(
                tasks,
                read_rows,
                executor,
                request.cancelled,
                settings=request.fanout_settings,
                on_outcome=request.on_outcome,
                observer=measurements,
            )
        return replace(run, history_rows_read=physical_rows, timing=measurements.snapshot())


@dataclass(frozen=True)
class _TrainingTaskFactory:
    selection: SelectedTrainingRequest
    history: SQLiteHistoryTrainingInputSnapshot
    resources: ExitStack
    measurements: TrainingExecutionMeasurements

    sqlite_policy: SqliteIoPolicy = field(kw_only=True)

    def prepare(
        self, profile: ScoringProfileId, progress: TomorrowTrainingProgressPort
    ) -> SelectedTrainingTask[SelectedProfileResult]:
        try:
            return self._v1(progress) if profile == "v1" else self._standard(profile, progress)
        except InterruptedError:
            raise
        except (OSError, RuntimeError, ValueError, ImportError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            return SelectedTrainingTask(profile, frozenset(), partial(_failed, reason or type(exc).__name__))

    def _v1(self, progress: TomorrowTrainingProgressPort) -> SelectedTrainingTask[SelectedProfileResult]:
        selection = self.selection
        paths = selection.paths
        calendar = self.history.snapshot.calendar.open_dates
        settings = LoadV1TrainingSettings(selection.runtime.project_root)
        reused = InspectV1Training(
            self.history, paths.models, settings, cancel_requested=selection.cancelled,
            compute_threads=selection.cpu_budget.profile_threads.v1,
        )
        if reused is not None:
            return SelectedTrainingTask("v1", frozenset(), partial(_reused, reused))
        splits = tuple(BuildLearnedRankDateSplit(calendar, settings, maturity_sessions=h) for h in (1, 5))
        first = min(calendar.index(day) for split in splits for day in split.sample_dates) - settings.history_sessions
        dates = AlignmentReadDates(calendar, frozenset(calendar[first:]))
        execute = partial(
            RunV1Training,
            lambda: self.history,
            paths.models,
            sample_root=paths.samples,
            history_root=paths.history,
            settings=settings,
            sample_settings=selection.sample_settings,
            progress=progress,
            cancel_requested=selection.cancelled,
            memory_admission=self.measurements.memory_admission("v1", selection.memory_admission),
            cpu_admission=self.measurements.cpu_admission("v1", selection.cpu_budget),
            compute_threads=selection.cpu_budget.profile_threads.v1,
            execution_options=selection.execution_options,
            sqlite_policy=self.sqlite_policy,
        )
        return SelectedTrainingTask("v1", dates, partial(_run_v1, execute))

    def _standard(
        self, profile: ScoringProfileId, progress: TomorrowTrainingProgressPort
    ) -> SelectedTrainingTask[SelectedProfileResult]:
        selection = self.selection
        paths = selection.paths
        contract = next(
            item
            for item in (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE, V5_TRAINING_PROFILE)
            if item.profile_id == profile
        )
        settings = LoadProfileTrainingSettings(selection.runtime.project_root, profile)
        if isinstance(settings.fitting, TcnFittingParameters):
            # Load the optional native library before capturing job-wide thread limits.
            from trader.training.infra.profile.v5.network import PreserveTcnRuntime

            self.resources.enter_context(PreserveTcnRuntime())
            logging.getLogger(__name__).warning(
                "V5训练配置 | 拟合日 %s | 隐藏通道 %s | 精度 %s | 线程 %s | 最大轮数 %s "
                "| 早停耐心 %s | 相对改善阈值 %s",
                settings.fit_dates,
                settings.fitting.hidden_channels,
                settings.fitting.compute_dtype,
                settings.fitting.compute_threads,
                settings.fitting.epochs,
                settings.fitting.early_stopping_rounds,
                settings.fitting.early_stopping_relative_delta,
            )
        request, cache, dates, reused = InspectSelectedProfile(
            self.history,
            ProfileTrainingRequest(
                paths.history,
                paths.models,
                contract,
                contract.heads,
                settings,
                progress,
                sample_root=paths.samples,
                sample_settings=selection.sample_settings,
                memory_admission=self.measurements.memory_admission(profile, selection.memory_admission),
                cpu_admission=self.measurements.cpu_admission(profile, selection.cpu_budget),
                compute_threads=settings.fitting.compute_threads if isinstance(settings.fitting, TcnFittingParameters)
                else selection.cpu_budget.profile_threads.for_profile(profile),
                execution_options=selection.execution_options,
                memory_capacity_bytes=selection.memory_capacity_bytes,
            ), sqlite_policy=self.sqlite_policy,
        )
        if reused is not None:
            return SelectedTrainingTask(profile, dates, partial(_reused, reused))
        return SelectedTrainingTask(profile, dates, partial(FitSelectedProfile, self.history, request, cache, sqlite_policy=self.sqlite_policy))
