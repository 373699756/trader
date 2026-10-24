"""Stable past-only calibration and diagnostic prediction statistics."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from trader.recommendation.domain.market.factors import AverageRankPercentiles

PREDICTION_EVALUATION_CONTRACT = "centered_calibration_two_dates_confirmation_predictions_sealed_holdout"
MINIMUM_CALIBRATION_VARIANCE = 1e-20


class CalibrationUnavailableError(ValueError):
    """Only a demonstrably unusable calibration rejects one industry's fit."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class CalibrationResult:
    rows: int
    dates: int
    prediction_variance: float
    intercept: float
    slope: float


@dataclass
class CalibrationAccumulator:
    rows: int = 0
    prediction_mean: float = 0.0
    target_mean: float = 0.0
    prediction_m2: float = 0.0
    covariance_sum: float = 0.0
    days: set[date] = field(default_factory=set)

    def add(self, predictions: np.ndarray, targets: np.ndarray, dates: tuple[date, ...]) -> None:
        if predictions.ndim != 1 or predictions.shape != targets.shape or len(predictions) != len(dates):
            raise ValueError("calibration_prediction_shape_invalid")
        if not np.all(np.isfinite(predictions)) or not np.all(np.isfinite(targets)):
            raise CalibrationUnavailableError("calibration_nonfinite_or_invalid")
        count = len(predictions)
        if count == 0:
            return
        mean_x, mean_y = float(predictions.mean()), float(targets.mean())
        centered_x, centered_y = predictions - mean_x, targets - mean_y
        total = self.rows + count
        delta_x, delta_y = mean_x - self.prediction_mean, mean_y - self.target_mean
        weight = self.rows * count / total
        self.prediction_m2 += float(centered_x @ centered_x) + delta_x * delta_x * weight
        self.covariance_sum += float(centered_x @ centered_y) + delta_x * delta_y * weight
        self.prediction_mean += delta_x * count / total
        self.target_mean += delta_y * count / total
        self.rows = total
        self.days.update(dates)

    def finish(self) -> CalibrationResult:
        if self.rows < 2 or len(self.days) < 2:
            raise CalibrationUnavailableError("calibration_samples_or_dates_insufficient")
        variance = self.prediction_m2 / self.rows
        if not math.isfinite(variance) or variance <= MINIMUM_CALIBRATION_VARIANCE:
            raise CalibrationUnavailableError("calibration_prediction_variance_insufficient")
        slope = self.covariance_sum / self.prediction_m2
        intercept = self.target_mean - slope * self.prediction_mean
        if not math.isfinite(slope) or not math.isfinite(intercept):
            raise CalibrationUnavailableError("calibration_nonfinite_or_invalid")
        return CalibrationResult(self.rows, len(self.days), variance, intercept, slope)


@dataclass(frozen=True)
class PredictionMetrics:
    input_rows: int
    predicted_rows: int
    covered_codes: int
    covered_dates: int
    covered_industries: int
    mean_absolute_error: float | None
    root_mean_squared_error: float | None
    mean_daily_rank_ic: float | None
    rank_ic_dates: int


@dataclass
class PredictionMetricsAccumulator:
    input_rows: int = 0
    predicted_rows: int = 0
    absolute_error: float = 0.0
    squared_error: float = 0.0
    codes: set[str] = field(default_factory=set)
    days: set[date] = field(default_factory=set)
    industries: set[str] = field(default_factory=set)
    current_day: date | None = None
    daily_predictions: list[float] = field(default_factory=list)
    daily_targets: list[float] = field(default_factory=list)
    rank_ic_sum: float = 0.0
    rank_ic_dates: int = 0

    def add(self, day: date, code: str, industry: str, target: float, prediction: float | None) -> None:
        self.input_rows += 1
        if prediction is None:
            return
        if not math.isfinite(target) or not math.isfinite(prediction):
            raise ValueError("validation_predictions_nonfinite")
        if self.current_day is not None and day < self.current_day:
            raise ValueError("validation_dates_not_ordered")
        if self.current_day is not None and day != self.current_day:
            self._finish_day()
        self.current_day = day
        self.predicted_rows += 1
        error = prediction - target
        self.absolute_error += abs(error)
        self.squared_error += error * error
        self.codes.add(code)
        self.days.add(day)
        self.industries.add(industry)
        self.daily_predictions.append(prediction)
        self.daily_targets.append(target)

    def _finish_day(self) -> None:
        if len(self.daily_targets) >= 2:
            predicted = np.asarray(AverageRankPercentiles(self.daily_predictions))
            target = np.asarray(AverageRankPercentiles(self.daily_targets))
            predicted -= predicted.mean()
            target -= target.mean()
            divisor = math.sqrt(float(predicted @ predicted) * float(target @ target))
            if divisor > 0:
                self.rank_ic_sum += float(predicted @ target) / divisor
                self.rank_ic_dates += 1
        self.daily_predictions.clear()
        self.daily_targets.clear()
        self.current_day = None

    def finish(self) -> PredictionMetrics:
        self._finish_day()
        if not math.isfinite(self.absolute_error) or not math.isfinite(self.squared_error):
            raise ValueError("validation_errors_nonfinite")
        return PredictionMetrics(
            self.input_rows,
            self.predicted_rows,
            len(self.codes),
            len(self.days),
            len(self.industries),
            self.absolute_error / self.predicted_rows if self.predicted_rows else None,
            math.sqrt(self.squared_error / self.predicted_rows) if self.predicted_rows else None,
            self.rank_ic_sum / self.rank_ic_dates if self.rank_ic_dates else None,
            self.rank_ic_dates,
        )


__all__ = [
    "PREDICTION_EVALUATION_CONTRACT",
    "MINIMUM_CALIBRATION_VARIANCE",
    "CalibrationUnavailableError",
    "CalibrationResult",
    "CalibrationAccumulator",
    "PredictionMetrics",
    "PredictionMetricsAccumulator",
]
