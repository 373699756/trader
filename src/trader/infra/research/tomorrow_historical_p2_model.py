"""Deterministic ridge/LightGBM trainer for the isolated Tomorrow P2 screen."""

from __future__ import annotations

import importlib
from collections.abc import Iterator, Sequence
from typing import Protocol, cast

import lightgbm as lgb

from trader.application.research.tomorrow_historical_p2_screening import (
    TOMORROW_HISTORICAL_P2_ALPHA_FEATURE_IDS,
    TomorrowHistoricalP2ModelArtifact,
    TomorrowHistoricalP2ModelFit,
    TomorrowHistoricalP2Row,
)
from trader.domain.research.tomorrow_historical_p2 import TomorrowHistoricalP2Candidate


class _Array(Protocol):
    def mean(self, axis: int) -> _Array: ...

    def std(self, axis: int) -> _Array: ...

    def reshape(self, *shape: int) -> _Array: ...

    def __iter__(self) -> Iterator[float]: ...

    def __len__(self) -> int: ...

    def __getitem__(self, key: object) -> object: ...

    def __sub__(self, other: object) -> _Array: ...

    def __truediv__(self, other: object) -> _Array: ...

    def __matmul__(self, other: object) -> _Array: ...

    def __add__(self, other: object) -> _Array: ...

    def __mul__(self, other: object) -> _Array: ...

    def __le__(self, other: object) -> _Array: ...


class _Linalg(Protocol):
    def solve(self, left: _Array, right: _Array) -> _Array: ...


class _Numpy(Protocol):
    float64: object
    linalg: _Linalg

    def asarray(self, value: object, *, dtype: object) -> _Array: ...

    def eye(self, size: int, *, dtype: object) -> _Array: ...

    def where(self, condition: object, left: object, right: object) -> _Array: ...

    def transpose(self, value: _Array) -> _Array: ...


_NUMPY = cast(_Numpy, importlib.import_module("numpy"))


class TomorrowHistoricalP2EnsembleTrainer:
    def fit(
        self,
        training: tuple[TomorrowHistoricalP2Row, ...],
        validation: tuple[TomorrowHistoricalP2Row, ...],
        candidate: TomorrowHistoricalP2Candidate,
    ) -> TomorrowHistoricalP2ModelFit:
        internal_start = _internal_validation_start(training)
        fitting = training[:internal_start]
        internal = training[internal_start:]
        fitting_x = _matrix(fitting)
        internal_x = _matrix(internal)
        training_x = _matrix(training)
        validation_x = _matrix(validation)
        means = fitting_x.mean(axis=0)
        scales = fitting_x.std(axis=0)
        scales = _NUMPY.where(scales <= 0.0, 1.0, scales)
        fitting_z = (fitting_x - means) / scales
        internal_z = (internal_x - means) / scales
        training_z = (training_x - means) / scales
        validation_z = (validation_x - means) / scales
        fitting_y = _targets(fitting)
        internal_y = _targets(internal)
        linear_intercept, linear_coefficients = _fit_ridge(fitting_z, fitting_y, candidate.linear_ridge)
        linear_training = _linear_predict(training_z, linear_intercept, linear_coefficients)
        linear_validation = _linear_predict(validation_z, linear_intercept, linear_coefficients)
        booster = _fit_lightgbm(fitting_z, fitting_y, internal_z, internal_y, candidate)
        iteration = booster.best_iteration or booster.current_iteration()
        tree_training = _prediction(booster, training_z, iteration)
        tree_validation = _prediction(booster, validation_z, iteration)
        weight = candidate.model_weights[0]
        artifact = TomorrowHistoricalP2ModelArtifact(
            candidate_id=candidate.candidate_id,
            feature_ids=TOMORROW_HISTORICAL_P2_ALPHA_FEATURE_IDS,
            transformer_means=_floats(means),
            transformer_scales=_floats(scales),
            linear_intercept=linear_intercept,
            linear_coefficients=linear_coefficients,
            lightgbm_model=booster.model_to_string(num_iteration=iteration),
            lightgbm_best_iteration=iteration,
            training_rows=len(training),
            internal_validation_rows=len(internal),
        )
        return TomorrowHistoricalP2ModelFit(
            artifact=artifact,
            training_predictions=tuple(
                weight * linear + (1.0 - weight) * tree
                for linear, tree in zip(linear_training, tree_training, strict=True)
            ),
            validation_predictions=tuple(
                weight * linear + (1.0 - weight) * tree
                for linear, tree in zip(linear_validation, tree_validation, strict=True)
            ),
            validation_model_disagreement=tuple(
                abs(linear - tree) for linear, tree in zip(linear_validation, tree_validation, strict=True)
            ),
        )


def _internal_validation_start(rows: tuple[TomorrowHistoricalP2Row, ...]) -> int:
    dates = tuple(sorted({row.trade_date for row in rows}))
    if len(dates) < 5:
        raise ValueError("Tomorrow P2 training requires at least five chronological dates")
    internal_dates = max(1, len(dates) // 5)
    boundary = dates[-internal_dates]
    return next(index for index, row in enumerate(rows) if row.trade_date >= boundary)


def _matrix(rows: Sequence[TomorrowHistoricalP2Row]) -> _Array:
    return _NUMPY.asarray(tuple(row.alpha_features for row in rows), dtype=_NUMPY.float64)


def _targets(rows: Sequence[TomorrowHistoricalP2Row]) -> _Array:
    return _NUMPY.asarray(tuple(row.gross_excess_return for row in rows), dtype=_NUMPY.float64)


def _fit_ridge(matrix: _Array, targets: _Array, ridge: float) -> tuple[float, tuple[float, ...]]:
    feature_mean = matrix.mean(axis=0)
    target_values = _floats(targets)
    target_mean = sum(target_values) / len(target_values)
    centered_x = matrix - feature_mean
    centered_y = targets - target_mean
    width = len(feature_mean)
    transposed = _NUMPY.transpose(centered_x)
    coefficients = _NUMPY.linalg.solve(
        transposed @ centered_x + _NUMPY.eye(width, dtype=_NUMPY.float64) * ridge,
        transposed @ centered_y,
    )
    values = _floats(coefficients)
    intercept = target_mean - sum(value * mean for value, mean in zip(values, _floats(feature_mean), strict=True))
    return intercept, values


def _fit_lightgbm(
    fitting_x: _Array,
    fitting_y: _Array,
    internal_x: _Array,
    internal_y: _Array,
    candidate: TomorrowHistoricalP2Candidate,
) -> lgb.Booster:
    params: dict[str, str | int | float | bool] = {
        "objective": "regression_l2",
        "metric": "l2",
        "learning_rate": candidate.lightgbm_learning_rate,
        "max_depth": candidate.lightgbm_max_depth,
        "num_leaves": candidate.lightgbm_num_leaves,
        "min_data_in_leaf": candidate.lightgbm_min_data_in_leaf,
        "feature_fraction": 1.0,
        "bagging_fraction": 1.0,
        "bagging_freq": 0,
        "max_bin": 63,
        "deterministic": True,
        "force_col_wise": True,
        "num_threads": candidate.lightgbm_num_threads,
        "seed": candidate.model_random_seed,
        "feature_fraction_seed": candidate.model_random_seed,
        "bagging_seed": candidate.model_random_seed,
        "data_random_seed": candidate.model_random_seed,
        "verbosity": -1,
        "feature_pre_filter": False,
    }
    train = lgb.Dataset(
        fitting_x,
        label=fitting_y,
        feature_name=list(TOMORROW_HISTORICAL_P2_ALPHA_FEATURE_IDS),
        free_raw_data=True,
    )
    internal = lgb.Dataset(
        internal_x,
        label=internal_y,
        feature_name=list(TOMORROW_HISTORICAL_P2_ALPHA_FEATURE_IDS),
        reference=train,
        free_raw_data=True,
    )
    return lgb.train(
        params,
        train,
        num_boost_round=candidate.lightgbm_num_boost_round,
        valid_sets=[internal],
        valid_names=["training_internal_chronological_holdout"],
        callbacks=[lgb.early_stopping(candidate.lightgbm_early_stopping_rounds, verbose=False)],
    )


def _linear_predict(matrix: _Array, intercept: float, coefficients: tuple[float, ...]) -> tuple[float, ...]:
    values = matrix @ _NUMPY.asarray(coefficients, dtype=_NUMPY.float64) + intercept
    return _floats(values)


def _prediction(booster: lgb.Booster, rows: _Array, iteration: int) -> tuple[float, ...]:
    return _floats(_NUMPY.asarray(booster.predict(rows, num_iteration=iteration), dtype=_NUMPY.float64).reshape(-1))


def _floats(values: object) -> tuple[float, ...]:
    return tuple(float(value) for value in cast(_Array, values))


__all__ = ["TomorrowHistoricalP2EnsembleTrainer"]
