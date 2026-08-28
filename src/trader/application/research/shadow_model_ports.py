"""Typed compute protocol for offline Tomorrow shadow estimators."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal, Protocol

ShadowObjective = Literal["net_excess", "severe_loss"]
ShadowModelFamily = Literal["linear", "lightgbm"]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ShadowFitRequest:
    objective: ShadowObjective
    feature_names: tuple[str, ...]
    train_x: tuple[tuple[float, ...], ...]
    train_y: tuple[float, ...]
    validation_x: tuple[tuple[float, ...], ...]
    validation_y: tuple[float, ...]
    calibration_x: tuple[tuple[float, ...], ...]
    prediction_x: tuple[tuple[float, ...], ...]
    seed: int

    def __post_init__(self) -> None:
        if self.objective not in {"net_excess", "severe_loss"} or self.seed <= 0:
            raise ValueError("shadow fit identity is invalid")
        if not self.feature_names or len(set(self.feature_names)) != len(self.feature_names):
            raise ValueError("shadow fit feature names must be non-empty and unique")
        width = len(self.feature_names)
        for rows in (self.train_x, self.validation_x, self.calibration_x, self.prediction_x):
            if not rows or any(len(row) != width for row in rows):
                raise ValueError("shadow fit matrices must be non-empty with fixed width")
            if any(not math.isfinite(value) for row in rows for value in row):
                raise ValueError("shadow fit features must be finite")
        if len(self.train_x) != len(self.train_y) or len(self.validation_x) != len(self.validation_y):
            raise ValueError("shadow fit labels must match training and validation rows")
        if any(not math.isfinite(value) for value in (*self.train_y, *self.validation_y)):
            raise ValueError("shadow fit labels must be finite")
        if self.objective == "severe_loss" and any(
            value not in {0.0, 1.0} for value in (*self.train_y, *self.validation_y)
        ):
            raise ValueError("severe-loss labels must be binary")


@dataclass(frozen=True)
class ShadowFitResult:
    model_family: ShadowModelFamily
    model_hash: str
    calibration_predictions: tuple[float, ...]
    prediction_predictions: tuple[float, ...]

    def __post_init__(self) -> None:
        if self.model_family not in {"linear", "lightgbm"} or _SHA256.fullmatch(self.model_hash) is None:
            raise ValueError("shadow fit result identity is invalid")
        if not self.calibration_predictions or not self.prediction_predictions:
            raise ValueError("shadow fit predictions must be non-empty")
        if any(not math.isfinite(value) for value in (*self.calibration_predictions, *self.prediction_predictions)):
            raise ValueError("shadow fit predictions must be finite")


class ShadowModelTrainer(Protocol):
    @property
    def model_family(self) -> ShadowModelFamily: ...

    def fit_predict(self, request: ShadowFitRequest) -> ShadowFitResult: ...


__all__ = [
    "ShadowFitRequest",
    "ShadowFitResult",
    "ShadowModelFamily",
    "ShadowModelTrainer",
    "ShadowObjective",
]
