"""Training resource fixtures loaded from the sole production configuration."""

from functools import lru_cache
from pathlib import Path

from trader.infra.settings.training import LoadTrainingResourceSettings
from trader.training.application.resources import TrainingResourceSettings


@lru_cache(maxsize=1)
def TrainingResourceSettingsFixture() -> TrainingResourceSettings:
    return LoadTrainingResourceSettings(Path(__file__).resolve().parents[2], "hdd")
