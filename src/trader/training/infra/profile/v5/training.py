"""V5 adapter over the existing locked, recoverable dual-head training owner."""

from pathlib import Path

from trader.training.application.tomorrow_training import TomorrowTrainingProgressPort
from trader.training.infra.engine import ProfileTrainingRequest, RunProfileTraining, TrainingRunResult
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.training_settings import ProfileTrainingSettings


def RunV5Training(
    history_root: Path,
    model_root: Path,
    *,
    sample_root: Path,
    settings: ProfileTrainingSettings,
    progress: TomorrowTrainingProgressPort | None = None,
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
        )
    )


__all__ = ["RunV5Training"]
