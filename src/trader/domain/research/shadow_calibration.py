"""Pure deterministic linear models and shadow prediction calibrators."""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import fmean

_EPSILON = 1e-12
_PROBABILITY_EPSILON = 1e-9


@dataclass(frozen=True)
class LinearModel:
    intercept: float
    coefficients: tuple[float, ...]
    logistic: bool = False

    def __post_init__(self) -> None:
        if not math.isfinite(self.intercept) or not self.coefficients:
            raise ValueError("linear model identity is invalid")
        if any(not math.isfinite(value) for value in self.coefficients):
            raise ValueError("linear model coefficients must be finite")

    def predict(self, rows: tuple[tuple[float, ...], ...]) -> tuple[float, ...]:
        _validate_prediction_rows(rows, len(self.coefficients))
        raw = tuple(
            self.intercept + sum(weight * value for weight, value in zip(self.coefficients, row, strict=True))
            for row in rows
        )
        return tuple(_sigmoid(value) for value in raw) if self.logistic else raw


@dataclass(frozen=True)
class AffineCalibrator:
    intercept: float
    slope: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.intercept) or not math.isfinite(self.slope):
            raise ValueError("affine calibrator values must be finite")

    def predict(self, values: tuple[float, ...]) -> tuple[float, ...]:
        _finite(values, "affine calibration input")
        return tuple(self.intercept + self.slope * value for value in values)


@dataclass(frozen=True)
class PlattCalibrator:
    intercept: float
    slope: float
    constant: float | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.intercept) or not math.isfinite(self.slope):
            raise ValueError("Platt calibrator values must be finite")
        if self.constant is not None and (not math.isfinite(self.constant) or not 0.0 < self.constant < 1.0):
            raise ValueError("Platt constant must be in (0, 1)")

    def predict(self, probabilities: tuple[float, ...]) -> tuple[float, ...]:
        _probabilities(probabilities)
        if self.constant is not None:
            return tuple(self.constant for _value in probabilities)
        return tuple(_sigmoid(self.intercept + self.slope * _logit(value)) for value in probabilities)


def fit_ridge_model(
    rows: tuple[tuple[float, ...], ...],
    targets: tuple[float, ...],
    *,
    ridge: float,
) -> LinearModel:
    width = _validate_training(rows, targets, ridge)
    means = tuple(fmean(row[column] for row in rows) for column in range(width))
    target_mean = fmean(targets)
    centered = tuple(tuple(value - means[column] for column, value in enumerate(row)) for row in rows)
    matrix = [
        [sum(row[left] * row[right] for row in centered) + (ridge if left == right else 0.0) for right in range(width)]
        for left in range(width)
    ]
    vector = [
        sum(row[column] * (target - target_mean) for row, target in zip(centered, targets, strict=True))
        for column in range(width)
    ]
    coefficients = tuple(_solve(matrix, vector))
    intercept = target_mean - sum(weight * mean for weight, mean in zip(coefficients, means, strict=True))
    return LinearModel(intercept, coefficients)


def fit_logistic_model(
    rows: tuple[tuple[float, ...], ...],
    targets: tuple[float, ...],
    *,
    ridge: float,
) -> LinearModel:
    width = _validate_training(rows, targets, ridge)
    if any(target not in {0.0, 1.0} for target in targets):
        raise ValueError("logistic targets must be binary")
    positives = sum(targets)
    smoothed = (positives + 1.0) / (len(targets) + 2.0)
    parameters = [_logit(smoothed), *([0.0] * width)]
    if positives in {0.0, float(len(targets))}:
        return LinearModel(parameters[0], tuple(parameters[1:]), logistic=True)
    design = tuple((1.0, *row) for row in rows)
    for _iteration in range(50):
        fitted = tuple(
            _sigmoid(sum(value * weight for value, weight in zip(row, parameters, strict=True))) for row in design
        )
        gradient = [
            sum(
                row[column] * (probability - target)
                for row, probability, target in zip(design, fitted, targets, strict=True)
            )
            + (ridge * parameters[column] if column else 0.0)
            for column in range(width + 1)
        ]
        hessian = [
            [
                sum(
                    row[left] * row[right] * probability * (1.0 - probability)
                    for row, probability in zip(design, fitted, strict=True)
                )
                + (ridge if left == right and left else 0.0)
                + (_EPSILON if left == right else 0.0)
                for right in range(width + 1)
            ]
            for left in range(width + 1)
        ]
        step = _solve(hessian, gradient)
        parameters = [value - delta for value, delta in zip(parameters, step, strict=True)]
        if max(abs(delta) for delta in step) < 1e-10:
            break
    return LinearModel(parameters[0], tuple(parameters[1:]), logistic=True)


def fit_affine_calibrator(raw: tuple[float, ...], targets: tuple[float, ...]) -> AffineCalibrator:
    _paired(raw, targets, "affine calibration")
    raw_mean = fmean(raw)
    target_mean = fmean(targets)
    variance = sum((value - raw_mean) ** 2 for value in raw)
    if variance <= _EPSILON:
        return AffineCalibrator(target_mean, 0.0)
    covariance = sum((value - raw_mean) * (target - target_mean) for value, target in zip(raw, targets, strict=True))
    slope = covariance / variance
    return AffineCalibrator(target_mean - slope * raw_mean, slope)


def fit_platt_calibrator(probabilities: tuple[float, ...], targets: tuple[float, ...]) -> PlattCalibrator:
    _paired(probabilities, targets, "Platt calibration")
    _probabilities(probabilities)
    if any(target not in {0.0, 1.0} for target in targets):
        raise ValueError("Platt targets must be binary")
    positives = sum(targets)
    if positives in {0.0, float(len(targets))}:
        constant = (positives + 1.0) / (len(targets) + 2.0)
        return PlattCalibrator(_logit(constant), 0.0, constant)
    logits = tuple((_logit(value),) for value in probabilities)
    model = fit_logistic_model(logits, targets, ridge=1e-3)
    return PlattCalibrator(model.intercept, model.coefficients[0])


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(vector)
    augmented = [row[:] + [value] for row, value in zip(matrix, vector, strict=True)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) <= _EPSILON:
            raise ValueError("model matrix is singular")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row], augmented[column], strict=True)
            ]
    return [augmented[index][-1] for index in range(size)]


def _validate_training(rows: tuple[tuple[float, ...], ...], targets: tuple[float, ...], ridge: float) -> int:
    if not rows or len(rows) != len(targets) or not math.isfinite(ridge) or ridge <= 0.0:
        raise ValueError("model training input is invalid")
    width = len(rows[0])
    if width == 0 or any(len(row) != width for row in rows):
        raise ValueError("model training matrix width is invalid")
    _finite(tuple(value for row in rows for value in row), "model training feature")
    _finite(targets, "model training target")
    return width


def _validate_prediction_rows(rows: tuple[tuple[float, ...], ...], width: int) -> None:
    if any(len(row) != width for row in rows):
        raise ValueError("model prediction matrix width is invalid")
    _finite(tuple(value for row in rows for value in row), "model prediction feature")


def _paired(left: tuple[float, ...], right: tuple[float, ...], label: str) -> None:
    if not left or len(left) != len(right):
        raise ValueError(f"{label} inputs must be non-empty and paired")
    _finite(left, label)
    _finite(right, label)


def _finite(values: tuple[float, ...], label: str) -> None:
    if any(not math.isfinite(value) for value in values):
        raise ValueError(f"{label} must be finite")


def _probabilities(values: tuple[float, ...]) -> None:
    _finite(values, "probability")
    if any(not 0.0 <= value <= 1.0 for value in values):
        raise ValueError("probabilities must be in [0, 1]")


def _sigmoid(value: float) -> float:
    if value >= 0.0:
        inverse = math.exp(-value)
        return 1.0 / (1.0 + inverse)
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)


def _logit(value: float) -> float:
    clipped = min(1.0 - _PROBABILITY_EPSILON, max(_PROBABILITY_EPSILON, value))
    return math.log(clipped / (1.0 - clipped))


__all__ = [
    "AffineCalibrator",
    "LinearModel",
    "PlattCalibrator",
    "fit_affine_calibrator",
    "fit_logistic_model",
    "fit_platt_calibrator",
    "fit_ridge_model",
]
