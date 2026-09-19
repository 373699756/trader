"""V2-owned 251-session training command adapter."""

from datetime import datetime
from pathlib import Path

from trader.training.application.tomorrow_training import TomorrowTrainingProgressPort
from trader.training.infra.engine import ProfileTrainingRequest, TrainingRunResult, run_profile_training
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE


def run_v2_training(
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
            V2_TRAINING_PROFILE,
            V2_TRAINING_PROFILE.heads,
            progress,
            observed_at,
        )
    )


__all__ = ["run_v2_training"]
