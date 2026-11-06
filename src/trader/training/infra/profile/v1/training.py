"""Pinned V1 input, disposable samples and native fitting resource adapters."""

from __future__ import annotations

import platform
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, contextmanager, nullcontext
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

import numpy as np
from lightgbm.basic import LightGBMError

from trader.download.domain.history_revision import HistoryTrainingRow
from trader.download.infra.history_control_index import HistoryMaintenanceLock
from trader.download.infra.history_partition_repack import RequireHistoryRepackIdle
from trader.infra.serialization.canonical import ContentHash
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.recommendation.domain.publication.models import Strategy
from trader.training.application import learned_rank_training
from trader.training.application.resources import (
    TrainingCpuAdmission, TrainingExecutionObserver, TrainingMemoryAdmission, TrainingSampleSettings,
)
from trader.training.application.tomorrow_training import (
    TOMORROW_TRAINING_COMPUTE_THREADS, TomorrowTrainingProgressPort,
)
from trader.training.domain.final_fit import FINAL_FIT_CONTRACT, FinalFitDatePlan
from trader.training.domain.v1.learned_rank import (
    LEARNED_RANK_CONTRACT, LearnedRankDateSplit, LearnedRankFinalHead, LearnedRankFinalPair,
    LearnedRankHead, V1TrainingSettings,
)
from trader.training.domain.v1.training_plan import LearnedRankTrainingPlan, LearnedRankTrainingSource
from trader.training.infra.execution_options import BASELINE_EXECUTION_OPTIONS, TrainingExecutionOptions
from trader.training.infra.fitting_resources import ReserveTrainingFittingResources
from trader.training.infra.history.history_training_input import HistoryTrainingInputSnapshot
from trader.training.infra.implementation_identity import TrainingImplementationIdentity
from trader.training.infra.profile.v1.fitting import FitLearnedRankHead, LearnedRankPartitions, RefitLearnedRankHead
from trader.training.infra.profile.v1.models import (
    LearnedRankPredictor, PublishLearnedRankPair, ReadLearnedRankPair, RecoverLearnedRankPublication,
)
from trader.training.infra.profile.v1.samples import (
    BuildLearnedRankSamples, CheckV1Cancellation, LearnedRankInputPort, LearnedRankMatrixRequest,
    LearnedRankSampleIndex, LearnedRankSampleStream,
)
from trader.training.infra.sample_cache import TemporaryTrainingSamples


def _training_identity(
    snapshot: HistoryTrainingInputSnapshot,
    settings: V1TrainingSettings,
    compute_threads: int = TOMORROW_TRAINING_COMPUTE_THREADS,
) -> str:
    return ContentHash((
        snapshot,
        settings,
        compute_threads,
        LEARNED_RANK_CONTRACT,
        FINAL_FIT_CONTRACT,
        TrainingImplementationIdentity(),
        (platform.python_version(), version("numpy"), version("lightgbm")),
    ))


def _training_source(
    source: LearnedRankInputPort, settings: V1TrainingSettings, compute_threads: int,
) -> LearnedRankTrainingSource:
    snapshot = source.snapshot
    return LearnedRankTrainingSource(
        snapshot.snapshot_hash, snapshot.source_identity_hash, snapshot.calendar_hash, snapshot.source_cutoff,
        snapshot.calendar.open_dates, _training_identity(snapshot, settings, compute_threads),
    )


@contextmanager
def _operation_boundary() -> Iterator[None]:
    try:
        yield
    except (InterruptedError, learned_rank_training.LearnedRankTrainingOperationError):
        raise
    except (OSError, RuntimeError, ValueError, sqlite3.Error, LightGBMError, np.linalg.LinAlgError) as exc:
        known_reasons = {
            "learned_rank_calendar_insufficient",
            "learned_rank_sample_partition_empty",
            "learned_rank_training_rows_insufficient",
            "learned_rank_validation_predictions_invalid",
            "learned-rank prediction metrics are invalid",
            "learned-rank head is invalid",
            "learned-rank pair provenance is invalid",
        }
        reason = str(exc)
        failure = reason if reason in known_reasons else type(exc).__name__
        raise learned_rank_training.LearnedRankTrainingOperationError(failure) from exc


@dataclass(frozen=True)
class _LearnedRankInspection:
    source: LearnedRankTrainingSource
    settings: V1TrainingSettings
    model_root: Path
    cancel_requested: Callable[[], bool] | None

    def check_cancelled(self) -> None:
        CheckV1Cancellation(self.cancel_requested)

    def read_pair(self) -> LearnedRankFinalPair | None:
        RecoverLearnedRankPublication(self.model_root)
        try:
            pair = ReadLearnedRankPair(self.model_root)
        except InterruptedError:
            raise
        except (OSError, ValueError, LightGBMError):
            return None
        return pair if isinstance(pair, LearnedRankFinalPair) else None

    def validate_pair(self, pair: LearnedRankFinalPair) -> bool:
        try:
            for head in pair.heads:
                self.check_cancelled()
                LearnedRankPredictor(head)
        except InterruptedError:
            raise
        except (OSError, ValueError, LightGBMError):
            return False
        return True


@dataclass(frozen=True)
class LearnedRankTrainingAdapter:
    input_factory: Callable[[], LearnedRankInputPort]
    model_root: Path
    sample_root: Path
    sample_settings: TrainingSampleSettings
    history_root: Path
    settings: V1TrainingSettings
    progress: TomorrowTrainingProgressPort | None
    cancel_requested: Callable[[], bool] | None
    aligned_rows: Iterator[HistoryTrainingRow] | None
    memory_admission: TrainingMemoryAdmission | None
    sqlite_policy: SqliteIoPolicy
    cpu_admission: TrainingCpuAdmission | None
    compute_threads: int
    execution_options: TrainingExecutionOptions
    execution_observer: TrainingExecutionObserver | None

    @contextmanager
    def open(self) -> Iterator[learned_rank_training.LearnedRankTrainingSessionPort]:
        with (
            _operation_boundary(),
            HistoryMaintenanceLock(self.history_root.with_name(".history.lock"))
            if self.aligned_rows is None else nullcontext(),
            HistoryMaintenanceLock(self.model_root / ".v1.training.lock")
            if self.aligned_rows is None else nullcontext(),
        ):
            CheckV1Cancellation(self.cancel_requested)
            RequireHistoryRepackIdle(self.history_root)
            input_source = self.input_factory()
            yield _PinnedLearnedRankSession(
                _training_source(input_source, self.settings, self.compute_threads), self.settings,
                self.model_root, self.cancel_requested, self, input_source,
            )


@dataclass(frozen=True)
class _PinnedLearnedRankSession(_LearnedRankInspection):
    adapter: LearnedRankTrainingAdapter
    input_source: LearnedRankInputPort

    @property
    def progress(self) -> TomorrowTrainingProgressPort | None:
        return self.adapter.progress

    @contextmanager
    def samples(self) -> Iterator[learned_rank_training.LearnedRankSamplesPort]:
        with TemporaryTrainingSamples(self.adapter.sample_root, "v1") as directory:
            yield _LearnedRankSamples(self, directory)

    def publish_pair(self, pair: LearnedRankFinalPair) -> None:
        PublishLearnedRankPair(self.model_root, pair)


@dataclass(frozen=True)
class _LearnedRankSamples:
    session: _PinnedLearnedRankSession
    directory: Path

    def build(self, plan: LearnedRankTrainingPlan) -> learned_rank_training.PreparedLearnedRankSamples:
        adapter = self.session.adapter
        sample_path = self.directory / "samples.sqlite3"
        with closing(LearnedRankSampleIndex(
            sample_path, sample_settings=adapter.sample_settings, sqlite_policy=adapter.sqlite_policy,
        )) as index:
            with adapter.execution_observer.sample_build("v1") if adapter.execution_observer is not None else nullcontext():
                counts = BuildLearnedRankSamples(
                    self.session.input_source,
                    plan.splits,
                    index,
                    history_sessions=plan.settings.history_sessions,
                    stream=LearnedRankSampleStream(adapter.progress, adapter.cancel_requested, adapter.aligned_rows),
                    final_fit_plans=plan.final_fit_plans,
                )
            return learned_rank_training.PreparedLearnedRankSamples(
                counts, index.coverage(plan.splits, final_fit_plans=plan.final_fit_plans), sample_path.stat().st_size,
            )

    @contextmanager
    def fitting(self, required_bytes: int) -> Iterator[learned_rank_training.LearnedRankFittingPort]:
        adapter = self.session.adapter
        with (
            closing(LearnedRankSampleIndex(
                self.directory / "samples.sqlite3", sample_settings=adapter.sample_settings,
                read_only=True, sqlite_policy=adapter.sqlite_policy,
            )) as index,
            ReserveTrainingFittingResources(
                adapter.cpu_admission,
                adapter.memory_admission,
                adapter.compute_threads,
                lambda: required_bytes,
                self.session.check_cancelled,
                memory_first=adapter.execution_options.memory_first,
            ) as reservation,
        ):
            yield _LearnedRankFitter(index, self.directory, adapter, reservation.compute_threads)


@dataclass(frozen=True)
class _LearnedRankFitter:
    index: LearnedRankSampleIndex
    directory: Path
    adapter: LearnedRankTrainingAdapter
    compute_threads: int

    def fit_diagnostic(self, strategy: Strategy, split: LearnedRankDateSplit) -> LearnedRankHead:
        requests = tuple(
            LearnedRankMatrixRequest(dates, strategy)
            for dates in (split.fitting, split.early_stopping, split.calibration, split.validation)
        )
        with ExitStack() as resources:
            matrices = self.index.read_matrices(
                requests, self.directory / "matrices", resources, cancel_requested=self.adapter.cancel_requested,
            )
            return FitLearnedRankHead(
                strategy, split, LearnedRankPartitions(*matrices), self.adapter.settings,
                cancel_requested=self.adapter.cancel_requested, compute_threads=self.compute_threads,
            )

    def refit(self, diagnostic: LearnedRankHead, plan: FinalFitDatePlan) -> LearnedRankFinalHead:
        with ExitStack() as resources:
            matrices = self.index.read_matrices(
                (LearnedRankMatrixRequest(plan.fit_dates, diagnostic.strategy),),
                self.directory / "matrices", resources, cancel_requested=self.adapter.cancel_requested,
            )
            return RefitLearnedRankHead(
                diagnostic, plan, matrices[0], self.adapter.settings,
                compute_threads=self.compute_threads, cancel_requested=self.adapter.cancel_requested,
            )


def InspectV1Training(
    source: LearnedRankInputPort,
    model_root: Path,
    settings: V1TrainingSettings,
    *,
    cancel_requested: Callable[[], bool] | None = None,
    compute_threads: int = TOMORROW_TRAINING_COMPUTE_THREADS,
) -> learned_rank_training.V1TrainingResult | None:
    """Validate the stored native pair against a pinned source and implementation."""
    if type(compute_threads) is not int or compute_threads < 1:
        raise ValueError("training_cpu_request_invalid")
    CheckV1Cancellation(cancel_requested)
    return learned_rank_training.InspectLearnedRankTraining(_LearnedRankInspection(
        _training_source(source, settings, compute_threads), settings, model_root, cancel_requested,
    ))


def RunV1Training(
    input_factory: Callable[[], LearnedRankInputPort],
    model_root: Path,
    *,
    sample_root: Path,
    sample_settings: TrainingSampleSettings,
    history_root: Path,
    settings: V1TrainingSettings,
    progress: TomorrowTrainingProgressPort | None = None,
    cancel_requested: Callable[[], bool] | None = None,
    aligned_rows: Iterator[HistoryTrainingRow] | None = None,
    memory_admission: TrainingMemoryAdmission | None = None,
    sqlite_policy: SqliteIoPolicy,
    cpu_admission: TrainingCpuAdmission | None = None,
    compute_threads: int = TOMORROW_TRAINING_COMPUTE_THREADS,
    execution_options: TrainingExecutionOptions = BASELINE_EXECUTION_OPTIONS,
    execution_observer: TrainingExecutionObserver | None = None,
) -> learned_rank_training.V1TrainingResult:
    """Hold the pinned source and all disposable resources for the application run."""
    if type(compute_threads) is not int or compute_threads < 1:
        raise ValueError("training_cpu_request_invalid")
    return learned_rank_training.TrainLearnedRank(LearnedRankTrainingAdapter(
        input_factory, model_root, sample_root, sample_settings, history_root, settings,
        progress, cancel_requested, aligned_rows, memory_admission, sqlite_policy, cpu_admission,
        compute_threads, execution_options, execution_observer,
    ))


__all__ = [
    "LearnedRankTrainingAdapter",
    "InspectV1Training",
    "RunV1Training",
]
