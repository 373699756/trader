"""V2-owned 251-session training command adapter."""

from trader.infra.sqlite_policy import SqliteIoPolicy

from pathlib import Path

from trader.training.application.tomorrow_training import TomorrowTrainingProgressPort
from trader.training.infra.engine import ProfileTrainingRequest, RunProfileTraining, TrainingRunResult
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.training_settings import ProfileTrainingSettings


def RunV2Training(
    history_root: Path,
    train_root: Path,
    *,
    settings: ProfileTrainingSettings,
    sample_root: Path,
    progress: TomorrowTrainingProgressPort | None = None, sqlite_policy: SqliteIoPolicy,
) -> TrainingRunResult:
    return RunProfileTraining(
        ProfileTrainingRequest(
            history_root,
            train_root,
            V2_TRAINING_PROFILE,
            V2_TRAINING_PROFILE.heads,
            settings,
            progress,
            sample_root=sample_root,
        ), sqlite_policy=sqlite_policy
    )


__all__ = ["RunV2Training"]
