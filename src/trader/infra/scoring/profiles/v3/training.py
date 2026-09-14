"""V3-owned 61-session training and archive-repack command adapters."""

from datetime import datetime
from pathlib import Path

from trader.application.research.tomorrow_training import TomorrowTrainingProgressPort
from trader.infra.scoring.profiles.v3.contracts import TOMORROW_HEAD_CONTRACT, V3_TRAINING_PROFILE
from trader.infra.scoring.training.engine import (
    HeadTrainingResult,
    ProfileTrainingRequest,
    TrainingRunResult,
    run_profile_training,
    run_repack_profile_training,
)


def run_tomorrow_training(
    history_root: Path,
    train_root: Path,
    *,
    progress: TomorrowTrainingProgressPort | None = None,
    observed_at: datetime | None = None,
) -> HeadTrainingResult:
    return run_profile_training(
        ProfileTrainingRequest(
            history_root,
            train_root,
            V3_TRAINING_PROFILE,
            (TOMORROW_HEAD_CONTRACT,),
            progress,
            observed_at,
        )
    ).heads[0]


def run_v3_training(
    history_root: Path,
    train_root: Path,
    *,
    progress: TomorrowTrainingProgressPort | None = None,
    observed_at: datetime | None = None,
) -> TrainingRunResult:
    return run_profile_training(
        ProfileTrainingRequest(
            history_root,
            train_root,
            V3_TRAINING_PROFILE,
            V3_TRAINING_PROFILE.heads,
            progress,
            observed_at,
        )
    )


def run_repack_tomorrow_training(
    history_root: Path,
    train_root: Path,
    *,
    expected_history_snapshot_hash: str,
    progress: TomorrowTrainingProgressPort | None = None,
) -> HeadTrainingResult:
    return run_repack_profile_training(
        ProfileTrainingRequest(
            history_root,
            train_root,
            V3_TRAINING_PROFILE,
            (TOMORROW_HEAD_CONTRACT,),
            progress,
            expected_history_snapshot_hash=expected_history_snapshot_hash,
        )
    ).heads[0]


def run_repack_v3_training(
    history_root: Path,
    train_root: Path,
    *,
    expected_history_snapshot_hash: str,
    progress: TomorrowTrainingProgressPort | None = None,
) -> TrainingRunResult:
    return run_repack_profile_training(
        ProfileTrainingRequest(
            history_root,
            train_root,
            V3_TRAINING_PROFILE,
            V3_TRAINING_PROFILE.heads,
            progress,
            expected_history_snapshot_hash=expected_history_snapshot_hash,
        )
    )


__all__ = [
    "run_repack_tomorrow_training",
    "run_repack_v3_training",
    "run_tomorrow_training",
    "run_v3_training",
]
