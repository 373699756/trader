"""Explicit, bounded offline V1 training; atomic publication only after both heads succeed."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
from lightgbm.basic import LightGBMError

from trader.download.domain.history_revision import HistoryTrainingRow
from trader.download.infra.history_control_index import HistoryMaintenanceLock
from trader.download.infra.history_partition_repack import RequireHistoryPartitionRepackInactive
from trader.infra.serialization.canonical import ContentHash
from trader.recommendation.domain.publication.models import Strategy
from trader.training.application.resources import TrainingMemoryAdmission
from trader.training.application.tomorrow_training import (
    TomorrowTrainingProgress,
    TomorrowTrainingProgressPort,
    TomorrowTrainingProgressState,
    TomorrowTrainingStage,
)
from trader.training.domain.v1.learned_rank import (
    LEARNED_RANK_CONTRACT,
    BuildLearnedRankDateSplit,
    LearnedRankHead,
    LearnedRankPair,
    V1TrainingSettings,
)
from trader.training.infra.profile.v1.fitting import FitLearnedRankHead, LearnedRankPartitions
from trader.training.infra.profile.v1.models import PublishLearnedRankPair
from trader.training.infra.profile.v1.samples import (
    BuildLearnedRankSamples,
    CheckV1Cancellation,
    LearnedRankInputPort,
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
    heads: tuple[LearnedRankHead, ...] = ()
    sample_counts: LearnedRankSampleCounts | None = None
    failure_reasons: tuple[str, ...] = ()
    coverage: tuple[LearnedRankPartitionCoverage, ...] = ()


def RunV1Training(
    input_factory: Callable[[], LearnedRankInputPort],
    model_root: Path,
    *,
    sample_root: Path,
    history_root: Path,
    settings: V1TrainingSettings,
    progress: TomorrowTrainingProgressPort | None = None,
    cancel_requested: Callable[[], bool] | None = None,
    aligned_rows: Iterator[HistoryTrainingRow] | None = None,
    memory_admission: TrainingMemoryAdmission | None = None,
) -> V1TrainingResult:
    """Consume a pinned input; all disposable writes stay in the selected sample root."""

    run_id = input_hash = ""
    counts: LearnedRankSampleCounts | None = None
    peak_bytes = 0
    heads: list[LearnedRankHead] = []
    coverage: tuple[LearnedRankPartitionCoverage, ...] = ()
    try:
        with (
            HistoryMaintenanceLock(history_root.with_name(".history.lock")) if aligned_rows is None else nullcontext(),
            HistoryMaintenanceLock(model_root / ".v1.training.lock") if aligned_rows is None else nullcontext(),
        ):
            CheckV1Cancellation(cancel_requested)
            RequireHistoryPartitionRepackInactive(history_root)
            source = input_factory()
            snapshot = source.snapshot
            input_hash = snapshot.active_snapshot_hash
            run_id = ContentHash((snapshot, settings, LEARNED_RANK_CONTRACT))
            calendar = snapshot.calendar.open_dates
            splits = (
                BuildLearnedRankDateSplit(calendar, settings, maturity_sessions=1),
                BuildLearnedRankDateSplit(calendar, settings, maturity_sessions=5),
            )
            with TemporaryTrainingSamples(sample_root, "v1") as directory, ExitStack() as fitting_resources:
                sample_path = directory / "samples.sqlite3"
                with closing(LearnedRankSampleIndex(sample_path)) as index:
                    counts = BuildLearnedRankSamples(
                        source,
                        splits,
                        index,
                        stream=LearnedRankSampleStream(progress, cancel_requested, aligned_rows),
                    )
                    peak_bytes = sample_path.stat().st_size
                    coverage = index.coverage(splits)
                    failures = tuple(
                        f"learned_rank_sample_partition_empty:{item.strategy.value}:{item.partition}"
                        if item.rows == 0
                        else f"learned_rank_training_rows_insufficient:{item.strategy.value}:{item.partition}"
                        for item in coverage
                        if item.rows == 0
                        or (item.partition == "fitting" and item.rows < settings.minimum_training_rows)
                    )
                    if failures:
                        return V1TrainingResult(
                            "failed", run_id, input_hash, peak_bytes, (), counts, failures, coverage
                        )
                if memory_admission is not None:
                    fitting_resources.enter_context(
                        memory_admission.reserve(
                            counts.samples * 512 + 256 * 1024**2, lambda: CheckV1Cancellation(cancel_requested)
                        )
                    )
                with closing(LearnedRankSampleIndex(sample_path, read_only=True)) as index:
                    for strategy, split in zip((Strategy.TOMORROW, Strategy.D25), splits, strict=True):
                        CheckV1Cancellation(cancel_requested)
                        _progress(progress, "model_fit", "started", strategy)
                        with ExitStack() as resources:
                            matrices = tuple(
                                index.matrix(dates, strategy, directory / strategy.value / name, resources)
                                for name, dates in (
                                    ("fitting", split.fitting),
                                    ("early_stopping", split.early_stopping),
                                    ("calibration", split.calibration),
                                    ("validation", split.validation),
                                )
                            )
                            heads.append(
                                FitLearnedRankHead(
                                    strategy,
                                    split,
                                    LearnedRankPartitions(*matrices),
                                    settings,
                                    cancel_requested=cancel_requested,
                                )
                            )
                        _progress(progress, "model_fit", "completed", strategy)
                _progress(progress, "artifact_publish", "started", None)
                pair = LearnedRankPair(
                    snapshot.active_snapshot_hash,
                    snapshot.source_identity_hash,
                    snapshot.calendar_hash,
                    snapshot.source_cutoff,
                    settings,
                    (heads[0], heads[1]),
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
                stage, state, completed, 1, produced_units=completed if stage == "model_fit" else 0, strategy=strategy
            )
        )


__all__ = ["RunV1Training", "V1TrainingResult"]
