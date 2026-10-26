"""Pinned history and per-profile adapters for the shared streaming use case."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from concurrent.futures import Executor
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import date
from functools import partial

from trader.download.domain.history_revision import HistoryTrainingRow
from trader.download.infra.history_control_index import HistoryMaintenanceLock
from trader.download.infra.history_partition_repack import RequireHistoryPartitionRepackInactive
from trader.infra.settings import RuntimeSettings
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.application.resources import TrainingMemoryAdmission
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
from trader.training.infra.history.aligned_training_rows import AlignmentReadDates, AlignTrainingRows
from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot
from trader.training.infra.profile.v1.settings import LoadV1TrainingSettings
from trader.training.infra.profile.v1.training import InspectV1Training, RunV1Training, V1TrainingResult
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.profile.v5.settings import TcnFittingParameters
from trader.training.infra.training_settings import LoadProfileTrainingSettings

SelectedProfileResult = TrainingRunResult | V1TrainingResult


def _run_v1(execute: Callable[..., V1TrainingResult], rows: Iterator[HistoryTrainingRow]) -> V1TrainingResult:
    return execute(aligned_rows=rows)


def _reused(result: SelectedProfileResult, _rows: Iterator[HistoryTrainingRow]) -> SelectedProfileResult:
    return result


def _failed(reason: str, _rows: Iterator[HistoryTrainingRow]) -> SelectedProfileResult:
    raise ValueError(reason)


@dataclass(frozen=True)
class SelectedTrainingRequest:
    runtime: RuntimeSettings
    profiles: tuple[ScoringProfileId, ...]
    progresses: tuple[TomorrowTrainingProgressPort, ...]
    cancelled: Callable[[], bool]
    memory_admission: TrainingMemoryAdmission
    paths: TrainingLocations
    on_outcome: Callable[[SelectedTrainingOutcome[SelectedProfileResult]], None] | None = None


def TrainSelectedProfiles(
    request: SelectedTrainingRequest, executor: Executor
) -> SelectedTrainingRun[SelectedProfileResult]:
    paths = request.paths
    with ExitStack() as scope:
        scope.enter_context(HistoryMaintenanceLock(paths.history.with_name(".history.lock")))
        for profile in request.profiles:
            scope.enter_context(HistoryMaintenanceLock(paths.models / f".{profile}.training.lock"))
        RequireHistoryPartitionRepackInactive(paths.history)
        archive = SQLiteHistoryTrainingInputSnapshot.open(paths.history)
        factory = _TrainingTaskFactory(request, archive)
        tasks = tuple(
            factory.prepare(profile, progress)
            for profile, progress in zip(request.profiles, request.progresses, strict=True)
        )
        physical_rows = 0

        def source_rows(dates: frozenset[date]) -> Iterator[HistoryTrainingRow]:
            nonlocal physical_rows
            for row in archive.iter_training_rows(dates):
                physical_rows += 1
                yield row

        def read_rows(dates: frozenset[date]) -> Iterator[HistoryTrainingRow]:
            readable = AlignmentReadDates(archive.snapshot.calendar.open_dates, dates)
            yield from AlignTrainingRows(
                source_rows(readable), tuple(sorted(readable)), archive.snapshot.listing_intervals
            )

        run = RunSelectedTrainingTasks(tasks, read_rows, executor, request.cancelled, on_outcome=request.on_outcome)
        return replace(run, history_rows_read=physical_rows)


@dataclass(frozen=True)
class _TrainingTaskFactory:
    selection: SelectedTrainingRequest
    archive: SQLiteHistoryTrainingInputSnapshot

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
        calendar = self.archive.snapshot.calendar.open_dates
        settings = LoadV1TrainingSettings(selection.runtime.project_root)
        reused = InspectV1Training(self.archive, paths.models, settings, cancel_requested=selection.cancelled)
        if reused is not None:
            return SelectedTrainingTask("v1", frozenset(), partial(_reused, reused))
        splits = tuple(BuildLearnedRankDateSplit(calendar, settings, maturity_sessions=h) for h in (1, 5))
        first = min(calendar.index(day) for split in splits for day in split.sample_dates) - 251
        dates = AlignmentReadDates(calendar, frozenset(calendar[first:]))
        execute = partial(
            RunV1Training,
            lambda: self.archive,
            paths.models,
            sample_root=paths.samples,
            history_root=paths.history,
            settings=settings,
            progress=progress,
            cancel_requested=selection.cancelled,
            memory_admission=selection.memory_admission,
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
            from trader.training.infra.profile.v5.network import ConfigureTcnRuntime

            ConfigureTcnRuntime(training=True, compute_threads=settings.fitting.compute_threads)
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
            self.archive,
            ProfileTrainingRequest(
                paths.history,
                paths.models,
                contract,
                contract.heads,
                settings,
                progress,
                sample_root=paths.samples,
                memory_admission=selection.memory_admission,
            ),
        )
        if reused is not None:
            return SelectedTrainingTask(profile, dates, partial(_reused, reused))
        return SelectedTrainingTask(profile, dates, partial(FitSelectedProfile, self.archive, request, cache))
