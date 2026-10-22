"""Explicit, bounded offline V1 training; atomic publication only after both heads succeed."""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Callable
from contextlib import ExitStack, closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
from lightgbm.basic import LightGBMError

from trader.download.infra.history_control_index import HistoryMaintenanceLock
from trader.download.infra.history_partition_repack import RequireHistoryPartitionRepackInactive
from trader.infra.process_lock import ProcessLock
from trader.infra.serialization.canonical import ContentHash
from trader.recommendation.domain.publication.models import Strategy
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
from trader.training.infra.profile.v1.artifacts import PublishLearnedRankPair
from trader.training.infra.profile.v1.fitting import FitLearnedRankHead, LearnedRankPartitions
from trader.training.infra.profile.v1.samples import (
    BuildLearnedRankSamples,
    CheckV1Cancellation,
    LearnedRankInputPort,
    LearnedRankSampleCounts,
    LearnedRankSampleIndex,
)


@dataclass(frozen=True)
class V1TrainingResult:
    status: Literal["engineering_ready", "failed"]
    run_id: str
    training_input_hash: str
    sample_database_peak_bytes: int
    heads: tuple[LearnedRankHead, ...] = ()
    sample_counts: LearnedRankSampleCounts | None = None
    failure_reasons: tuple[str, ...] = ()


def RunV1Training(
    input_factory: Callable[[], LearnedRankInputPort],
    model_root: Path,
    *,
    sample_root: Path,
    history_root: Path,
    settings: V1TrainingSettings,
    progress: TomorrowTrainingProgressPort | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> V1TrainingResult:
    """Consume a pinned input; all disposable writes stay in the selected sample root."""

    run_id = input_hash = ""
    counts: LearnedRankSampleCounts | None = None
    peak_bytes = 0
    heads: list[LearnedRankHead] = []
    try:
        with (
            HistoryMaintenanceLock(history_root.with_name(".history.lock")),
            ProcessLock(model_root / ".learned_rank.training.lock", label="V1 training"),
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
            root = sample_root / "learned_rank"
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="training-", dir=root) as temporary:
                directory = Path(temporary)
                sample_path = directory / "samples.sqlite3"
                with closing(LearnedRankSampleIndex(sample_path)) as index:
                    counts = BuildLearnedRankSamples(
                        source, splits, index, progress=progress, cancel_requested=cancel_requested
                    )
                    peak_bytes = sample_path.stat().st_size
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
                _progress(progress, "artifact_publish", "completed", Strategy.D25)
        return V1TrainingResult("engineering_ready", run_id, input_hash, peak_bytes, tuple(heads), counts)
    except InterruptedError:
        raise
    except (OSError, RuntimeError, ValueError, sqlite3.Error, LightGBMError, np.linalg.LinAlgError) as exc:
        return V1TrainingResult("failed", run_id, input_hash, peak_bytes, (), counts, (type(exc).__name__,))


def _progress(
    progress: TomorrowTrainingProgressPort | None,
    stage: TomorrowTrainingStage,
    state: TomorrowTrainingProgressState,
    strategy: Strategy,
) -> None:
    if progress is not None:
        progress.publish(TomorrowTrainingProgress(stage, state, int(state == "completed"), 1, strategy=strategy))


__all__ = ["RunV1Training", "V1TrainingResult"]
