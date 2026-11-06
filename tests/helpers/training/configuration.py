"""Typed fitting overrides over the current training configuration."""

from __future__ import annotations

from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from typing_extensions import Unpack

from trader.training.domain.v1.learned_rank import V1TrainingSettings
from trader.training.infra.profile.v1.settings import LoadV1TrainingSettings
from trader.training.infra.profile.v5.settings import TcnComputeDtype, TcnFittingParameters
from trader.training.infra.training_settings import LoadProfileTrainingSettings

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class _V1Changes(TypedDict, total=False):
    fit_dates: int
    early_stopping_dates: int
    calibration_dates: int
    validation_dates: int
    embargo_dates: int
    minimum_training_rows: int
    learning_rate: float
    max_depth: int
    num_leaves: int
    minimum_leaf_rows: int
    boosting_rounds: int
    early_stopping_rounds: int
    maximum_bins: int
    histogram_pool_mib: int
    seed: int
    ridge_penalty: float
    ranking_round_trip_cost: float


class _TcnChanges(TypedDict, total=False):
    minimum_industry_training_rows: int
    learning_rate: float
    weight_decay: float
    dropout: float
    batch_size: int
    epochs: int
    early_stopping_rounds: int
    seed: int
    hidden_channels: int
    compute_dtype: TcnComputeDtype
    early_stopping_relative_delta: float
    compute_threads: int


@lru_cache(maxsize=1)
def _v1_settings() -> V1TrainingSettings:
    return LoadV1TrainingSettings(PROJECT_ROOT)


@lru_cache(maxsize=1)
def _tcn_settings() -> TcnFittingParameters:
    settings = LoadProfileTrainingSettings(PROJECT_ROOT, "v5").fitting
    assert isinstance(settings, TcnFittingParameters)
    return settings


def V1TrainingSettingsFixture(
    *, settings: V1TrainingSettings | None = None, **changes: Unpack[_V1Changes],
) -> V1TrainingSettings:
    return replace(settings if settings is not None else _v1_settings(), **changes)


def TcnFittingParametersFixture(
    *, settings: TcnFittingParameters | None = None, **changes: Unpack[_TcnChanges],
) -> TcnFittingParameters:
    return replace(settings if settings is not None else _tcn_settings(), **changes)
