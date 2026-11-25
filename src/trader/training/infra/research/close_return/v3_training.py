"""Train both V3 research heads against the exact activated archive snapshot."""

from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.download.domain.current_industry import CurrentIndustrySnapshot
from trader.training.application.resources import TrainingSampleSettings

from pathlib import Path

from trader.training.application.tomorrow_training import TomorrowTrainingProgressPort
from trader.training.infra.engine import ProfileTrainingRequest, RunRepackProfileTraining
from trader.training.application.profile_training import TrainingRunResult
from trader.training.infra.research.close_return.v3_contracts import V3_TRAINING_PROFILE
from trader.training.infra.training_settings import ProfileTrainingSettings


def RunRepackV3Training(
    history_root: Path,
    train_root: Path,
    *,
    settings: ProfileTrainingSettings,
    sample_root: Path,
    sample_settings: TrainingSampleSettings,
    industry_snapshot: CurrentIndustrySnapshot,
    expected_history_snapshot_hash: str,
    progress: TomorrowTrainingProgressPort | None = None, sqlite_policy: SqliteIoPolicy,
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
            sample_settings=sample_settings,
            industry_snapshot=industry_snapshot,
            expected_history_snapshot_hash=expected_history_snapshot_hash,
        ), sqlite_policy=sqlite_policy
    )


__all__ = ["RunRepackV3Training"]
