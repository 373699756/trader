"""V5 adapter over the existing locked, recoverable dual-head training owner."""

from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.download.domain.current_industry import CurrentIndustrySnapshot
from trader.training.application.resources import TrainingSampleSettings

from pathlib import Path

from trader.training.application.tomorrow_training import TomorrowTrainingProgressPort
from trader.training.infra.engine import ProfileTrainingRequest, RunProfileTraining
from trader.training.application.profile_training import TrainingRunResult
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.training_settings import ProfileTrainingSettings


def RunV5Training(
    history_root: Path,
    model_root: Path,
    *,
    sample_root: Path,
    sample_settings: TrainingSampleSettings,
    industry_snapshot: CurrentIndustrySnapshot,
    settings: ProfileTrainingSettings,
    progress: TomorrowTrainingProgressPort | None = None, sqlite_policy: SqliteIoPolicy,
) -> TrainingRunResult:
    return RunProfileTraining(
        ProfileTrainingRequest(
            history_root,
            model_root,
            V5_TRAINING_PROFILE,
            V5_TRAINING_PROFILE.heads,
            settings,
            progress,
            sample_root=sample_root,
            sample_settings=sample_settings,
            industry_snapshot=industry_snapshot,
        ), sqlite_policy=sqlite_policy
    )


__all__ = ["RunV5Training"]
