"""V3 feature adapter over the shared profile-aware sample builder."""

from trader.training.application.tomorrow_training import TomorrowTrainingProgressPort, TomorrowTrainingWindow
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.profile.v3.training_sample_repository import SQLiteV3TrainingSampleRepository
from trader.training.infra.sample_builder import (
    TrainingSampleBuildRequest,
    TrainingWindowArchive,
    aligned_sample_dates,
    residualize_sample_day,
    training_alpha_target,
)
from trader.training.infra.sample_builder import build_training_samples as _build_training_samples


def build_training_samples(
    archive: TrainingWindowArchive,
    codes: tuple[str, ...],
    window: TomorrowTrainingWindow,
    repository: SQLiteV3TrainingSampleRepository,
    *,
    progress: TomorrowTrainingProgressPort | None = None,
) -> None:
    return _build_training_samples(
        TrainingSampleBuildRequest(
            archive,
            codes,
            window,
            repository,
            V3_TRAINING_PROFILE,
            progress,
        )
    )


__all__ = [
    "TrainingWindowArchive",
    "aligned_sample_dates",
    "build_training_samples",
    "residualize_sample_day",
    "training_alpha_target",
]
