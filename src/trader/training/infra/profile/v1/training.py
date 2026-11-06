"""Explicit, bounded offline V1 training; atomic publication only after both heads succeed."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.training.application.resources import TrainingSampleSettings

import platform
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, nullcontext
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Literal

import numpy as np
from lightgbm.basic import LightGBMError

from trader.download.domain.history_revision import HistoryTrainingRow
from trader.download.infra.history_control_index import HistoryMaintenanceLock
from trader.download.infra.history_partition_repack import RequireHistoryRepackIdle
from trader.infra.serialization.canonical import ContentHash
from trader.recommendation.domain.publication.models import Strategy
from trader.training.application.resources import TrainingCpuAdmission, TrainingExecutionObserver, TrainingMemoryAdmission
from trader.training.application.tomorrow_training import (
    TOMORROW_TRAINING_COMPUTE_THREADS,
    TomorrowTrainingProgress,
    TomorrowTrainingProgressPort,
    TomorrowTrainingProgressState,
    TomorrowTrainingStage,
)
from trader.training.domain.v1.learned_rank import (
    LEARNED_RANK_CONTRACT,
    BuildLearnedRankDateSplit,
    LearnedRankHead,
    LearnedRankFinalHead,
    LearnedRankFinalPair,
    V1TrainingSettings,
)
from trader.training.domain.final_fit import BuildFinalFitDatePlan, FINAL_FIT_CONTRACT
from trader.training.infra.history.history_training_input import HistoryTrainingInputSnapshot
from trader.training.infra.implementation_identity import TrainingImplementationIdentity
from trader.training.infra.execution_options import BASELINE_EXECUTION_OPTIONS, TrainingExecutionOptions
from trader.training.infra.fitting_resources import ReserveTrainingFittingResources
from trader.training.infra.profile.v1.fitting import FitLearnedRankHead, LearnedRankPartitions, RefitLearnedRankHead
from trader.training.infra.profile.v1.models import (
    LearnedRankPredictor,
    PublishLearnedRankPair,
    ReadLearnedRankPair,
    RecoverLearnedRankPublication,
)
from trader.training.infra.profile.v1.samples import (
    BuildLearnedRankSamples,
    CheckV1Cancellation,
    LearnedRankInputPort,
    LearnedRankMatrixRequest,
    LearnedRankPartitionCoverage,
    LearnedRankSampleCounts,
    LearnedRankSampleIndex,
    LearnedRankSampleStream,
)
from trader.training.infra.sample_cache import TemporaryTrainingSamples


@dataclass(frozen=True)
class V1TrainingResult:
    status: Literal["engineering_ready", "failed"]
    run_id: str
    training_input_hash: str
    sample_database_peak_bytes: int
    heads: tuple[LearnedRankFinalHead, ...] = ()
    sample_counts: LearnedRankSampleCounts | None = None
    failure_reasons: tuple[str, ...] = ()
    coverage: tuple[LearnedRankPartitionCoverage, ...] = ()
    reused: bool = False


def _training_identity(
    snapshot: HistoryTrainingInputSnapshot, settings: V1TrainingSettings,
    compute_threads: int = TOMORROW_TRAINING_COMPUTE_THREADS,
) -> str:
    return ContentHash(
        (
            snapshot,
            settings,
            compute_threads,
            LEARNED_RANK_CONTRACT,
            FINAL_FIT_CONTRACT,
            TrainingImplementationIdentity(),
            (platform.python_version(), version("numpy"), version("lightgbm")),
        )
    )


def InspectV1Training(
    source: LearnedRankInputPort,
    model_root: Path,
    settings: V1TrainingSettings,
    *,
    cancel_requested: Callable[[], bool] | None = None,
    compute_threads: int = TOMORROW_TRAINING_COMPUTE_THREADS,
) -> V1TrainingResult | None:
    """Reuse only a fully verified pair bound to this exact input and implementation."""
    if type(compute_threads) is not int or compute_threads < 1:
        raise ValueError("training_cpu_request_invalid")
    CheckV1Cancellation(cancel_requested)
    RecoverLearnedRankPublication(model_root)
    snapshot = source.snapshot
    identity = _training_identity(snapshot, settings, compute_threads)
    try:
        pair = ReadLearnedRankPair(model_root)
        if not isinstance(pair, LearnedRankFinalPair):
            return None
        if (
            pair.training_identity_hash != identity
            or pair.training_input_hash != snapshot.snapshot_hash
            or pair.source_identity_hash != snapshot.source_identity_hash
            or pair.calendar_hash != snapshot.calendar_hash
            or pair.source_cutoff != snapshot.source_cutoff
            or pair.settings != settings
            or pair.calendar_dates != snapshot.calendar.open_dates
            or tuple(head.split for head in pair.heads)
            != tuple(
                BuildLearnedRankDateSplit(
                    tuple(day for day in snapshot.calendar.open_dates if day <= snapshot.source_cutoff),
                    settings,
                    maturity_sessions=maturity,
                )
                for maturity in (1, 5)
            )
            or tuple(head.final_fit for head in pair.heads) != tuple(
                BuildFinalFitDatePlan(
                    snapshot.calendar.open_dates,
                    source_cutoff=snapshot.source_cutoff,
                    fit_sessions=settings.fit_dates,
                    maturity_sessions=maturity,
                )
                for maturity in (1, 5)
            )
        ):
            return None
        for head in pair.heads:
            CheckV1Cancellation(cancel_requested)
            LearnedRankPredictor(head)
    except InterruptedError:
        raise
    except (OSError, ValueError, LightGBMError):
        return None
    CheckV1Cancellation(cancel_requested)
    return V1TrainingResult("engineering_ready", identity, snapshot.snapshot_hash, 0, pair.heads, reused=True)


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
    memory_admission: TrainingMemoryAdmission | None = None, sqlite_policy: SqliteIoPolicy,
    cpu_admission: TrainingCpuAdmission | None = None,
    compute_threads: int = TOMORROW_TRAINING_COMPUTE_THREADS,
    execution_options: TrainingExecutionOptions = BASELINE_EXECUTION_OPTIONS,
    execution_observer: TrainingExecutionObserver | None = None,
) -> V1TrainingResult:
    """Consume a pinned input; all disposable writes stay in the selected sample root."""
    if type(compute_threads) is not int or compute_threads < 1:
        raise ValueError("training_cpu_request_invalid")

    run_id = input_hash = ""
    counts: LearnedRankSampleCounts | None = None
    peak_bytes = 0
    heads: list[LearnedRankFinalHead] = []
    coverage: tuple[LearnedRankPartitionCoverage, ...] = ()
    try:
        with (
            HistoryMaintenanceLock(history_root.with_name(".history.lock")) if aligned_rows is None else nullcontext(),
            HistoryMaintenanceLock(model_root / ".v1.training.lock") if aligned_rows is None else nullcontext(),
        ):
            CheckV1Cancellation(cancel_requested)
            RequireHistoryRepackIdle(history_root)
            source = input_factory()
            snapshot = source.snapshot
            input_hash = snapshot.snapshot_hash
            run_id = _training_identity(snapshot, settings, compute_threads)
            reused = InspectV1Training(
                source, model_root, settings, cancel_requested=cancel_requested, compute_threads=compute_threads,
            )
            if reused is not None:
                return reused
            calendar = snapshot.calendar.open_dates
            diagnostic_calendar = tuple(day for day in calendar if day <= snapshot.source_cutoff)
            splits = (
                BuildLearnedRankDateSplit(diagnostic_calendar, settings, maturity_sessions=1),
                BuildLearnedRankDateSplit(diagnostic_calendar, settings, maturity_sessions=5),
            )
            final_fit_plans = (
                BuildFinalFitDatePlan(
                    calendar, source_cutoff=snapshot.source_cutoff,
                    fit_sessions=settings.fit_dates, maturity_sessions=1,
                ),
                BuildFinalFitDatePlan(
                    calendar, source_cutoff=snapshot.source_cutoff,
                    fit_sessions=settings.fit_dates, maturity_sessions=5,
                ),
            )
            with TemporaryTrainingSamples(sample_root, "v1") as directory:
                sample_path = directory / "samples.sqlite3"
                with closing(LearnedRankSampleIndex(sample_path, sample_settings=sample_settings, sqlite_policy=sqlite_policy)) as index:
                    with execution_observer.sample_build("v1") if execution_observer is not None else nullcontext():
                        counts = BuildLearnedRankSamples(
                            source,
                            splits,
                            index,
                            history_sessions=settings.history_sessions,
                            stream=LearnedRankSampleStream(progress, cancel_requested, aligned_rows),
                            final_fit_plans=final_fit_plans,
                        )
                    peak_bytes = sample_path.stat().st_size
                    coverage = index.coverage(splits, final_fit_plans=final_fit_plans)
                    failures = tuple(
                        f"learned_rank_sample_partition_empty:{item.strategy.value}:{item.partition}"
                        if item.rows == 0
                        else f"learned_rank_final_fit_dates_insufficient:{item.strategy.value}"
                        if item.partition == "final_fitting" and item.effective_dates != item.expected_dates
                        else f"learned_rank_sample_dates_insufficient:{item.strategy.value}:{item.partition}"
                        if item.effective_dates != item.expected_dates
                        else f"learned_rank_training_rows_insufficient:{item.strategy.value}:{item.partition}"
                        for item in coverage
                        if item.rows == 0
                        or (item.partition in {"fitting", "final_fitting"} and item.rows < settings.minimum_training_rows)
                        or item.effective_dates != item.expected_dates
                    )
                    if failures:
                        return V1TrainingResult(
                            "failed", run_id, input_hash, peak_bytes, (), counts, failures, coverage
                        )
                strategies = (Strategy.TOMORROW, Strategy.D25)
                # Close each head's maps before opening the next head or final
                # phase, and keep only one native fitter in flight.
                required_bytes = max(
                    sum(
                        item.rows for item in coverage
                        if item.strategy is strategy and (item.partition == "final_fitting") == final_phase
                    )
                    for strategy in strategies for final_phase in (False, True)
                ) * 512 + 256 * 1024**2
                with (
                    closing(LearnedRankSampleIndex(sample_path, sample_settings=sample_settings, read_only=True, sqlite_policy=sqlite_policy)) as index,
                    ReserveTrainingFittingResources(
                        cpu_admission,
                        memory_admission,
                        compute_threads,
                        lambda: required_bytes,
                        lambda: CheckV1Cancellation(cancel_requested),
                        memory_first=execution_options.memory_first,
                    ) as reservation,
                ):
                    diagnostics: list[LearnedRankHead] = []
                    for strategy, split in zip(strategies, splits, strict=True):
                        requests = tuple(
                            LearnedRankMatrixRequest(dates, strategy)
                            for dates in (split.fitting, split.early_stopping, split.calibration, split.validation)
                        )
                        with ExitStack() as diagnostic_resources:
                            CheckV1Cancellation(cancel_requested)
                            _progress(progress, "model_fit", "started", strategy)
                            matrices = index.read_matrices(
                                requests, directory / "matrices", diagnostic_resources,
                                cancel_requested=cancel_requested,
                            )
                            diagnostics.append(
                                FitLearnedRankHead(
                                    strategy, split, LearnedRankPartitions(*matrices),
                                    settings, cancel_requested=cancel_requested, compute_threads=reservation.compute_threads,
                                )
                            )
                            _progress(progress, "model_fit", "completed", strategy)
                            del matrices
                    for diagnostic, plan in zip(diagnostics, final_fit_plans, strict=True):
                        with ExitStack() as final_resources:
                            _progress(progress, "final_model_refit", "started", diagnostic.strategy)
                            matrices = index.read_matrices(
                                (LearnedRankMatrixRequest(plan.fit_dates, diagnostic.strategy),),
                                directory / "matrices", final_resources, cancel_requested=cancel_requested,
                            )
                            heads.append(RefitLearnedRankHead(
                                diagnostic,
                                plan,
                                matrices[0],
                                settings,
                                compute_threads=reservation.compute_threads,
                                cancel_requested=cancel_requested,
                            ))
                            _progress(progress, "final_model_refit", "completed", diagnostic.strategy)
                            del matrices
                _progress(progress, "artifact_publish", "started", None)
                pair = LearnedRankFinalPair(
                    snapshot.snapshot_hash,
                    snapshot.source_identity_hash,
                    snapshot.calendar_hash,
                    snapshot.source_cutoff,
                    settings,
                    (heads[0], heads[1]),
                    run_id,
                    calendar,
                )
                CheckV1Cancellation(cancel_requested)
                PublishLearnedRankPair(model_root, pair)
                _progress(progress, "artifact_publish", "completed", None)
        return V1TrainingResult(
            "engineering_ready", run_id, input_hash, peak_bytes, tuple(heads), counts, coverage=coverage
        )
    except InterruptedError:
        raise
    except (OSError, RuntimeError, ValueError, sqlite3.Error, LightGBMError, np.linalg.LinAlgError) as exc:
        reason = str(exc)
        known_reasons = {
            "learned_rank_calendar_insufficient",
            "learned_rank_sample_partition_empty",
            "learned_rank_training_rows_insufficient",
            "learned_rank_validation_predictions_invalid",
            "learned-rank prediction metrics are invalid",
            "learned-rank head is invalid",
            "learned-rank pair provenance is invalid",
        }
        failure = reason if reason in known_reasons else type(exc).__name__
        return V1TrainingResult("failed", run_id, input_hash, peak_bytes, (), counts, (failure,), coverage)


def _progress(
    progress: TomorrowTrainingProgressPort | None,
    stage: TomorrowTrainingStage,
    state: TomorrowTrainingProgressState,
    strategy: Strategy | None,
) -> None:
    if progress is not None:
        completed = int(state == "completed")
        progress.publish(
            TomorrowTrainingProgress(
                stage, state, completed, 1,
                produced_units=completed if stage in {"model_fit", "final_model_refit"} else 0,
                strategy=strategy,
            )
        )


__all__ = ["InspectV1Training", "RunV1Training", "V1TrainingResult"]
