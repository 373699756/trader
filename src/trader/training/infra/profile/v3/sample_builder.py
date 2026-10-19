"""V3 feature adapter over the shared profile-aware sample builder."""

from trader.training.application.tomorrow_training import TomorrowTrainingProgressPort, TomorrowTrainingWindow
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.profile.v3.training_sample_index import SQLiteV3TrainingSampleIndex
from trader.training.infra.sample_builder import (
    AlignedSampleDates,
    ResidualizeSampleDay,
    TrainingAlphaTarget,
    TrainingHistorySource,
    TrainingSampleBuildRequest,
)
from trader.training.infra.sample_builder import BuildTrainingSamples as _build_training_samples


def BuildTrainingSamples(
    archive: TrainingHistorySource,
    codes: tuple[str, ...],
    window: TomorrowTrainingWindow,
    partition: SQLiteV3TrainingSampleIndex,
    *,
    progress: TomorrowTrainingProgressPort | None = None,
) -> None:
    return _build_training_samples(
        TrainingSampleBuildRequest(
            archive,
            codes,
            window,
            partition,
            V3_TRAINING_PROFILE,
            progress,
        )
    )


__all__ = [
    "TrainingHistorySource",
    "AlignedSampleDates",
    "BuildTrainingSamples",
    "ResidualizeSampleDay",
    "TrainingAlphaTarget",
]
