"""V3-owned 61-session training and archive-repack command adapters."""

from pathlib import Path

from trader.training.application.tomorrow_training import TomorrowTrainingProgressPort
from trader.training.infra.engine import (
    HeadTrainingResult,
    ProfileTrainingRequest,
    RunProfileTraining,
    RunRepackProfileTraining,
    TrainingRunResult,
)
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.training_settings import ProfileTrainingSettings


def RunTomorrowTraining(
    history_root: Path,
    train_root: Path,
    *,
    settings: ProfileTrainingSettings,
    sample_root: Path,
    progress: TomorrowTrainingProgressPort | None = None,
) -> HeadTrainingResult:
    return RunProfileTraining(
        ProfileTrainingRequest(
            history_root,
            train_root,
            V3_TRAINING_PROFILE,
            V3_TRAINING_PROFILE.heads,
            settings,
            progress,
            sample_root=sample_root,
        )
    ).heads[0]


def RunV3Training(
    history_root: Path,
    train_root: Path,
    *,
    settings: ProfileTrainingSettings,
    sample_root: Path,
    progress: TomorrowTrainingProgressPort | None = None,
) -> TrainingRunResult:
    return RunProfileTraining(
        ProfileTrainingRequest(
            history_root,
            train_root,
            V3_TRAINING_PROFILE,
            V3_TRAINING_PROFILE.heads,
            settings,
            progress,
            sample_root=sample_root,
        )
    )


def RunRepackTomorrowTraining(
    history_root: Path,
    train_root: Path,
    *,
    settings: ProfileTrainingSettings,
    sample_root: Path,
    expected_history_snapshot_hash: str,
    progress: TomorrowTrainingProgressPort | None = None,
) -> HeadTrainingResult:
    return RunRepackProfileTraining(
        ProfileTrainingRequest(
            history_root,
            train_root,
            V3_TRAINING_PROFILE,
            V3_TRAINING_PROFILE.heads,
            settings,
            progress,
            sample_root=sample_root,
            expected_history_snapshot_hash=expected_history_snapshot_hash,
        )
    ).heads[0]


def RunRepackV3Training(
    history_root: Path,
    train_root: Path,
    *,
    settings: ProfileTrainingSettings,
    sample_root: Path,
    expected_history_snapshot_hash: str,
    progress: TomorrowTrainingProgressPort | None = None,
) -> TrainingRunResult:
    return RunRepackProfileTraining(
        ProfileTrainingRequest(
            history_root,
            train_root,
            V3_TRAINING_PROFILE,
            V3_TRAINING_PROFILE.heads,
            settings,
            progress,
            sample_root=sample_root,
            expected_history_snapshot_hash=expected_history_snapshot_hash,
        )
    )


__all__ = [
    "RunRepackTomorrowTraining",
    "RunRepackV3Training",
    "RunTomorrowTraining",
    "RunV3Training",
]
