"""Pinned history and per-profile adapters for the shared streaming use case."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy
from dataclasses import field

import logging
import time
from collections.abc import Callable, Generator, Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime
from functools import partial
from pathlib import Path
from threading import Event
from threadpoolctl import ThreadpoolController

from trader.download.domain.history_revision import HistoryTrainingRow
from trader.download.domain.current_industry import CurrentIndustrySnapshot
from trader.download.infra.history_control_index import HistoryMaintenanceLock
from trader.download.infra.history_partition_repack import RequireHistoryRepackIdle
from trader.infra.settings import RuntimeSettings
from trader.infra.settings.data_paths import DataPaths
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.application.learned_rank_training import V1TrainingResult
from trader.training.application.resources import TrainingExecutionObserver, TrainingFanoutSettings, TrainingMemoryAdmission, TrainingSampleSettings
from trader.training.application.selected_training import (
    SelectedD25IndustryUnavailable,
    SelectedProfileResult,
    SelectedTrainingOutcome,
    SelectedTrainingPreparationError,
    SelectedTrainingRun,
    SelectedTrainingSessionPort,
    SelectedTrainingTask,
    SelectedTrainingDrainError,
    TrainingLocations,
)
from trader.training.application.tomorrow_training import CancellableTrainingProgress, TomorrowTrainingProgress, TomorrowTrainingProgressPort
from trader.infra.future_cleanup import CloseAfterFutures
from trader.infra.shutdown import ShutdownDeadline
from trader.training.domain.v1.training_plan import BuildLearnedRankReadDates
from trader.recommendation.domain.outcomes.prediction_target import PredictionTargets
from trader.training.domain.tomorrow_probability import TomorrowProbabilityProfileResult
from trader.training.infra.engine import FitSelectedProfile, InspectSelectedProfile, ProfileTrainingRequest
from trader.training.infra.execution_timing import TrainingExecutionMeasurements
from trader.training.infra.history.aligned_training_rows import AlignmentReadDates, AlignTrainingRows
from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot
from trader.training.infra.d25.contracts import StandardD25Profile
from trader.training.infra.learned_rank_settings import LoadV1TrainingSettings
from trader.training.infra.d25.v1.training import InspectV1Training, InspectV1TrainingDates, RunV1Training
from trader.training.infra.research.close_return.selection import CloseReturnResearchMode, ResearchCloseReturnProfile
from trader.training.infra.tcn.settings import TcnFittingParameters
from trader.training.infra.training_settings import LoadD25TrainingSettings, LoadProfileTrainingSettings
from trader.training.infra.cpu_admission import TrainingCpuBudget
from trader.training.infra.execution_options import TrainingExecutionOptions
from trader.training.infra.sample_progress import TrainingSampleProgressTracker
from trader.training.infra.tomorrow.training import OpenTomorrowHistoryInput, TomorrowTrainer


def ResolveTrainingLocations(
    paths: DataPaths, history_root: Path | None, training_root: Path | None,
) -> TrainingLocations:
    """Project configured paths and explicit CLI overrides into the training plan."""
    return TrainingLocations(
        history_root or paths.history,
        training_root / "models" if training_root else paths.models,
        training_root / "samples" if training_root else paths.samples,
        training_root / "maintenance.sqlite3" if training_root else paths.training_maintenance,
    )


def _run_v1(execute: Callable[..., V1TrainingResult], rows: Iterator[HistoryTrainingRow]) -> V1TrainingResult:
    return execute(aligned_rows=rows)


def _reused(result: SelectedProfileResult, _rows: Iterator[HistoryTrainingRow]) -> SelectedProfileResult:
    return result


def _run_with_native_limits(
    execute: Callable[[Iterator[HistoryTrainingRow]], SelectedProfileResult],
    controller: ThreadpoolController,
    threads: int,
    rows: Iterator[HistoryTrainingRow],
) -> SelectedProfileResult:
    # BLAS belongs to the job scope; each worker owns and restores its OpenMP state.
    with controller.limit(limits=threads, user_api="openmp"):
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
    begin_shutdown: Callable[[], ShutdownDeadline] | None = field(default=None, kw_only=True)
    strategy: Strategy | None = field(default=None, kw_only=True)


@dataclass(frozen=True)
class SelectedTrainingAdapter:
    request: SelectedTrainingRequest
    sqlite_policy: SqliteIoPolicy = field(kw_only=True)
    load_industry_snapshot: Callable[[datetime], CurrentIndustrySnapshot] = field(kw_only=True)
    clock: Callable[[], datetime] = field(kw_only=True)
    train_tomorrow: TomorrowTrainer | None = field(default=None, kw_only=True)
    prediction_targets: PredictionTargets | CloseReturnResearchMode = field(kw_only=True)
    _measurements: TrainingExecutionMeasurements = field(init=False)

    def __post_init__(self) -> None:
        if self.request.strategy is Strategy.LONG:
            raise ValueError("long has no training")
        if isinstance(self.prediction_targets, CloseReturnResearchMode):
            if self.request.strategy is not None or self.train_tomorrow is not None:
                raise ValueError("close-return research owns its own head selection")
        elif self.request.strategy is not Strategy.D25 and self.train_tomorrow is None:
            raise ValueError("tomorrow training requires its own trainer")
        object.__setattr__(
            self, "_measurements",
            self.request.measurements if self.request.measurements is not None
            else TrainingExecutionMeasurements(self.request.profiles, time.monotonic),
        )

    @property
    def profiles(self) -> tuple[ScoringProfileId, ...]:
        return self.request.profiles

    @property
    def strategy(self) -> Strategy | None:
        return self.request.strategy

    @property
    def progresses(self) -> tuple[TomorrowTrainingProgressPort, ...]:
        return self.request.progresses

    @property
    def cancelled(self) -> Callable[[], bool]:
        return self.request.cancelled

    @property
    def settings(self) -> TrainingFanoutSettings:
        return self.request.fanout_settings

    @property
    def observer(self) -> TrainingExecutionObserver:
        return self._measurements

    @property
    def on_outcome(self) -> Callable[[SelectedTrainingOutcome[SelectedProfileResult]], None] | None:
        return self.request.on_outcome

    def _hold_inputs(self, scope: ExitStack) -> None:
        paths = self.request.paths
        scope.enter_context(HistoryMaintenanceLock(paths.history.with_name(".history.lock")))
        for profile in sorted(self.request.profiles):
            scope.enter_context(HistoryMaintenanceLock(paths.models / f".{profile}.training.lock"))
        RequireHistoryRepackIdle(paths.history)

    def _fit_tomorrow(
        self, request: SelectedTrainingRequest,
    ) -> tuple[TomorrowProbabilityProfileResult, ...]:
        if request.strategy is Strategy.D25 or isinstance(self.prediction_targets, CloseReturnResearchMode):
            return ()
        train = self.train_tomorrow
        assert train is not None
        history = OpenTomorrowHistoryInput(request.paths.history, sqlite_policy=self.sqlite_policy)
        return train(
            history, request.paths.models, profiles=request.profiles, prediction_targets=self.prediction_targets,
            project_root=request.runtime.project_root, cancelled=request.cancelled,
            cpu_budget=request.cpu_budget, memory=request.memory_admission,
        )

    def run_tomorrow_only(self) -> SelectedTrainingRun[SelectedProfileResult]:
        with ExitStack() as scope:
            self._hold_inputs(scope)
            tomorrow = self._fit_tomorrow(self.request)
        return SelectedTrainingRun((), 0, 0, timing=self._measurements.snapshot(), tomorrow_probability=tomorrow)

    @contextmanager
    def open(self) -> Iterator[SelectedTrainingSessionPort[SelectedProfileResult]]:
        request, paths = self.request, self.request.paths
        stopped = Event()

        def cancelled() -> bool:
            return stopped.is_set() or self.request.cancelled()

        request = replace(request, cancelled=cancelled)
        scope = ExitStack()
        try:
            self._hold_inputs(scope)
            tomorrow = self._fit_tomorrow(request)
            try:
                industry = self.load_industry_snapshot(self.clock())
            except InterruptedError:
                raise
            except (OSError, ValueError, RuntimeError) as exc:
                if tomorrow:
                    raise SelectedD25IndustryUnavailable(tomorrow) from exc
                raise
            history = SQLiteHistoryTrainingInputSnapshot.open(
                paths.history, industry_snapshot=industry, sqlite_policy=self.sqlite_policy,
            )
            factory = _TrainingTaskFactory(
                request, history, scope, self._measurements, sqlite_policy=self.sqlite_policy,
                prediction_targets=self.prediction_targets,
            )
            yield _SelectedTrainingSession(request, history, factory, self._measurements, stopped, tomorrow)
        except SelectedTrainingDrainError as exc:
            cleanup = CloseAfterFutures(scope.pop_all().close, exc.pending)
            exc.pending = (cleanup,)
            raise
        finally:
            scope.close()


@dataclass
class _SelectedTrainingSession:
    request: SelectedTrainingRequest
    history: SQLiteHistoryTrainingInputSnapshot
    factory: _TrainingTaskFactory
    measurements: TrainingExecutionMeasurements
    stopped: Event
    tomorrow: tuple[TomorrowProbabilityProfileResult, ...]
    physical_rows: int = 0

    def abort(self) -> ShutdownDeadline:
        self.stopped.set()
        return (
            self.request.begin_shutdown() if self.request.begin_shutdown is not None
            else ShutdownDeadline.start(self.request.runtime.pipeline.shutdown_timeout_seconds)
        )

    def prepare_task(
        self, profile: ScoringProfileId, progress: TomorrowTrainingProgressPort,
    ) -> SelectedTrainingTask[SelectedProfileResult]:
        return self.factory.prepare(profile, CancellableTrainingProgress(progress, self.request.cancelled))

    @contextmanager
    def read_rows(self, dates: frozenset[date]) -> Iterator[Iterator[HistoryTrainingRow]]:
        readable = AlignmentReadDates(self.history.snapshot.calendar.open_dates, dates)
        with closing(self._source_rows(readable)) as source, closing(AlignTrainingRows(
            source, tuple(sorted(readable)), self.history.snapshot.listing_intervals,
        )) as rows:
            with closing(self._observe_input(rows, readable)) as observed:
                yield observed

    def _observe_input(
        self, rows: Iterator[HistoryTrainingRow], dates: frozenset[date],
    ) -> Generator[HistoryTrainingRow, None, None]:
        tracker = TrainingSampleProgressTracker(dates, min(dates), None)

        def report(*, finished: bool = False) -> None:
            value = tracker.snapshot(finished=finished)
            event = TomorrowTrainingProgress(
                "input_read", "running", value.completed_days, value.total_days, input_progress=value,
            )
            for progress in self.request.progresses:
                progress.publish(event)

        report()
        for row in rows:
            if tracker.observe(row.trade_date):
                report()
            yield row
        report(finished=True)

    def _source_rows(self, dates: frozenset[date]) -> Generator[HistoryTrainingRow, None, None]:
        with closing(self.history.iter_training_rows(dates)) as rows:
            for row in rows:
                self.physical_rows += 1
                yield row

    @contextmanager
    def native_runtime(
        self, tasks: tuple[SelectedTrainingTask[SelectedProfileResult], ...],
    ) -> Iterator[tuple[SelectedTrainingTask[SelectedProfileResult], ...]]:
        scope = ExitStack()
        controller = scope.enter_context(self.request.cpu_budget.native_runtime())
        try:
            yield tuple(replace(task, execute=partial(
                _run_with_native_limits, task.execute, controller, self.request.cpu_budget.native_threads,
            )) for task in tasks)
        except SelectedTrainingDrainError as exc:
            cleanup = CloseAfterFutures(scope.pop_all().close, exc.pending)
            exc.pending = (cleanup,)
            raise
        finally:
            scope.close()

    def completed(
        self, run: SelectedTrainingRun[SelectedProfileResult],
    ) -> SelectedTrainingRun[SelectedProfileResult]:
        return replace(
            run, history_rows_read=self.physical_rows, timing=self.measurements.snapshot(),
            industry_snapshot=self.history.snapshot.industry_snapshot,
            tomorrow_probability=self.tomorrow,
            d25_targets=tuple(
                (profile, self.factory.prediction_targets.for_profile(profile, Strategy.D25))
                for profile in self.request.profiles
            ) if isinstance(self.factory.prediction_targets, PredictionTargets) else (),
        )


@dataclass(frozen=True)
class _TrainingTaskFactory:
    selection: SelectedTrainingRequest
    history: SQLiteHistoryTrainingInputSnapshot
    resources: ExitStack
    measurements: TrainingExecutionMeasurements

    sqlite_policy: SqliteIoPolicy = field(kw_only=True)
    prediction_targets: PredictionTargets | CloseReturnResearchMode = field(kw_only=True)

    def prepare(
        self, profile: ScoringProfileId, progress: TomorrowTrainingProgressPort
    ) -> SelectedTrainingTask[SelectedProfileResult]:
        try:
            return self._v1(progress) if profile == "v1" else self._standard(profile, progress)
        except InterruptedError:
            raise
        except (OSError, RuntimeError, ValueError, ImportError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            raise SelectedTrainingPreparationError(reason or type(exc).__name__) from exc

    def _v1(self, progress: TomorrowTrainingProgressPort) -> SelectedTrainingTask[SelectedProfileResult]:
        selection = self.selection
        paths = selection.paths
        calendar = self.history.snapshot.calendar.open_dates
        settings = LoadV1TrainingSettings(selection.runtime.project_root)
        d25_target = (
            self.prediction_targets.for_profile("v1", Strategy.D25)
            if isinstance(self.prediction_targets, PredictionTargets) else None
        )
        strategies = (Strategy.D25,) if d25_target is not None else (Strategy.TOMORROW, Strategy.D25)
        reused = InspectV1Training(
            self.history, paths.models, settings, cancel_requested=selection.cancelled,
            compute_threads=selection.cpu_budget.profile_threads.v1,
            strategies=strategies,
            d25_target=d25_target,
        )
        if reused is not None:
            return SelectedTrainingTask("v1", frozenset(), partial(_reused, reused))
        blocked = InspectV1TrainingDates(
            self.history, settings, compute_threads=selection.cpu_budget.profile_threads.v1, strategies=strategies,
        )
        if blocked is not None:
            return SelectedTrainingTask("v1", frozenset(), partial(_reused, blocked))
        dates = AlignmentReadDates(calendar, BuildLearnedRankReadDates(
            calendar, self.history.snapshot.source_cutoff, settings, strategies=strategies,
        ))
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
            execution_observer=self.measurements,
            sqlite_policy=self.sqlite_policy,
            strategies=strategies,
            d25_target=d25_target,
        )
        return SelectedTrainingTask("v1", dates, partial(_run_v1, execute))

    def _standard(
        self, profile: ScoringProfileId, progress: TomorrowTrainingProgressPort
    ) -> SelectedTrainingTask[SelectedProfileResult]:
        selection = self.selection
        paths = selection.paths
        if isinstance(self.prediction_targets, PredictionTargets):
            contract = StandardD25Profile(profile)
            d25_target = self.prediction_targets.for_profile(profile, Strategy.D25)
            settings = LoadD25TrainingSettings(selection.runtime.project_root, profile)
        else:
            contract = ResearchCloseReturnProfile(profile)
            d25_target = None
            settings = LoadProfileTrainingSettings(selection.runtime.project_root, profile)
        if isinstance(settings.fitting, TcnFittingParameters):
            # Load the optional native library before capturing job-wide thread limits.
            from trader.training.infra.tcn.network import PreserveTcnRuntime

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
                industry_snapshot=self.history.snapshot.industry_snapshot,
                memory_admission=self.measurements.memory_admission(profile, selection.memory_admission),
                cpu_admission=self.measurements.cpu_admission(profile, selection.cpu_budget),
                compute_threads=settings.fitting.compute_threads if isinstance(settings.fitting, TcnFittingParameters)
                else selection.cpu_budget.profile_threads.for_profile(profile),
                execution_options=selection.execution_options,
                memory_capacity_bytes=selection.memory_capacity_bytes,
                execution_observer=self.measurements,
                d25_target=d25_target,
                cancel_requested=selection.cancelled,
            ), sqlite_policy=self.sqlite_policy,
        )
        if reused is not None:
            return SelectedTrainingTask(profile, dates, partial(_reused, reused))
        return SelectedTrainingTask(profile, dates, partial(FitSelectedProfile, self.history, request, cache, sqlite_policy=self.sqlite_policy))
