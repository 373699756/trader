"""Explicit, bounded offline V1 training; atomic publication only after both heads succeed."""

from __future__ import annotations

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
from trader.training.infra.history.history_training_input import HistoryTrainingInputSnapshot
from trader.training.infra.implementation_identity import TrainingImplementationIdentity
from trader.training.infra.profile.v1.fitting import FitLearnedRankHead, LearnedRankPartitions
from trader.training.infra.profile.v1.models import LearnedRankPredictor, PublishLearnedRankPair, ReadLearnedRankPair
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
    heads: tuple[LearnedRankHead, ...] = ()
    sample_counts: LearnedRankSampleCounts | None = None
    failure_reasons: tuple[str, ...] = ()
    coverage: tuple[LearnedRankPartitionCoverage, ...] = ()
    reused: bool = False


def _training_identity(snapshot: HistoryTrainingInputSnapshot, settings: V1TrainingSettings) -> str:
    return ContentHash(
        (
            snapshot,
            settings,
            LEARNED_RANK_CONTRACT,
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
) -> V1TrainingResult | None:
    """Reuse only a fully verified pair bound to this exact input and implementation."""
    CheckV1Cancellation(cancel_requested)
    snapshot = source.snapshot
    identity = _training_identity(snapshot, settings)
    try:
        pair = ReadLearnedRankPair(model_root)
        if (
            pair.training_identity_hash != identity
            or pair.training_input_hash != snapshot.snapshot_hash
            or pair.source_identity_hash != snapshot.source_identity_hash
            or pair.calendar_hash != snapshot.calendar_hash
            or pair.source_cutoff != snapshot.source_cutoff
            or pair.settings != settings
            or tuple(head.split for head in pair.heads)
            != tuple(
                BuildLearnedRankDateSplit(snapshot.calendar.open_dates, settings, maturity_sessions=maturity)
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
            RequireHistoryRepackIdle(history_root)
            source = input_factory()
            snapshot = source.snapshot
            input_hash = snapshot.snapshot_hash
            run_id = _training_identity(snapshot, settings)
            reused = InspectV1Training(source, model_root, settings, cancel_requested=cancel_requested)
            if reused is not None:
                return reused
            calendar = snapshot.calendar.open_dates
            splits = (
                BuildLearnedRankDateSplit(calendar, settings, maturity_sessions=1),
                BuildLearnedRankDateSplit(calendar, settings, maturity_sessions=5),
            )
            with TemporaryTrainingSamples(sample_root, "v1") as directory:
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
                strategies = (Strategy.TOMORROW, Strategy.D25)
                # Both raw mapped heads plus one native fit fit within the existing
                # 512-byte-per-row reservation; no second simultaneous fitter.
                required_bytes = max(
                    sum(item.rows for item in coverage if item.strategy is strategy) for strategy in strategies
                ) * 512 + 256 * 1024**2
                with (
                    closing(LearnedRankSampleIndex(sample_path, read_only=True)) as index,
                    memory_admission.reserve(required_bytes, lambda: CheckV1Cancellation(cancel_requested))
                    if memory_admission is not None else nullcontext(),
                    ExitStack() as resources,
                ):
                    requests = tuple(
                        LearnedRankMatrixRequest(dates, strategy)
                        for strategy, split in zip(strategies, splits, strict=True)
                        for dates in (split.fitting, split.early_stopping, split.calibration, split.validation)
                    )
                    matrices = index.read_matrices(
                        requests, directory / "matrices", resources, cancel_requested=cancel_requested
                    )
                    for number, (strategy, split) in enumerate(zip(strategies, splits, strict=True)):
                        CheckV1Cancellation(cancel_requested)
                        _progress(progress, "model_fit", "started", strategy)
                        heads.append(
                            FitLearnedRankHead(
                                strategy, split, LearnedRankPartitions(*matrices[number * 4:number * 4 + 4]),
                                settings, cancel_requested=cancel_requested,
                            )
                        )
                        _progress(progress, "model_fit", "completed", strategy)
                _progress(progress, "artifact_publish", "started", None)
                pair = LearnedRankPair(
                    snapshot.snapshot_hash,
                    snapshot.source_identity_hash,
                    snapshot.calendar_hash,
                    snapshot.source_cutoff,
                    settings,
                    (heads[0], heads[1]),
                    run_id,
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


__all__ = ["InspectV1Training", "RunV1Training", "V1TrainingResult"]
