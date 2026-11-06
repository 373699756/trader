"""Training resource fixtures loaded from the sole production configuration."""

from functools import lru_cache
from dataclasses import replace
from pathlib import Path

from trader.training.infra.resource_settings import LoadTrainingResourceSettings
from trader.training.application.resources import TrainingResourceSettings, TrainingSampleSettings


@lru_cache(maxsize=1)
def TrainingResourceSettingsFixture() -> TrainingResourceSettings:
    return LoadTrainingResourceSettings(Path(__file__).resolve().parents[3], "hdd")


def TrainingSampleSettingsFixture(**changes: int) -> TrainingSampleSettings:
    return replace(TrainingResourceSettingsFixture().samples, **changes)
