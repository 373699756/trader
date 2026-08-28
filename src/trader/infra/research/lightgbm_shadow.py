"""Deterministic shallow LightGBM adapter for offline shadow research."""

from __future__ import annotations

import hashlib
import importlib
from collections.abc import Iterator
from typing import Protocol, cast

import lightgbm as lgb

from trader.application.research.shadow_model_ports import ShadowFitRequest, ShadowFitResult, ShadowModelFamily


class _Array(Protocol):
    def reshape(self, *shape: int) -> _Array: ...

    def __iter__(self) -> Iterator[float]: ...


class _Numpy(Protocol):
    float64: object

    def asarray(self, value: object, *, dtype: object) -> _Array: ...


_NUMPY = cast(_Numpy, importlib.import_module("numpy"))


class LightGbmShadowTrainer:
    @property
    def model_family(self) -> ShadowModelFamily:
        return "lightgbm"

    def fit_predict(self, request: ShadowFitRequest) -> ShadowFitResult:
        params: dict[str, str | int | float | bool] = {
            "objective": "regression_l2" if request.objective == "net_excess" else "binary",
            "metric": "l2" if request.objective == "net_excess" else "binary_logloss",
            "learning_rate": 0.05,
            "max_depth": 3,
            "num_leaves": 7,
            "min_data_in_leaf": 20,
            "feature_fraction": 1.0,
            "bagging_fraction": 1.0,
            "bagging_freq": 0,
            "max_bin": 63,
            "deterministic": True,
            "force_col_wise": True,
            "num_threads": 1,
            "seed": request.seed,
            "feature_fraction_seed": request.seed,
            "bagging_seed": request.seed,
            "data_random_seed": request.seed,
            "verbosity": -1,
            "feature_pre_filter": False,
        }
        train = lgb.Dataset(
            _NUMPY.asarray(request.train_x, dtype=_NUMPY.float64),
            label=_NUMPY.asarray(request.train_y, dtype=_NUMPY.float64),
            feature_name=list(request.feature_names),
            free_raw_data=True,
        )
        validation = lgb.Dataset(
            _NUMPY.asarray(request.validation_x, dtype=_NUMPY.float64),
            label=_NUMPY.asarray(request.validation_y, dtype=_NUMPY.float64),
            feature_name=list(request.feature_names),
            reference=train,
            free_raw_data=True,
        )
        booster = lgb.train(
            params,
            train,
            num_boost_round=200,
            valid_sets=[validation],
            valid_names=["chronological_validation"],
            callbacks=[lgb.early_stopping(20, verbose=False)],
        )
        iteration = booster.best_iteration or booster.current_iteration()
        model_text = booster.model_to_string(num_iteration=iteration)
        calibration = _predictions(booster, request.calibration_x, iteration)
        prediction = _predictions(booster, request.prediction_x, iteration)
        return ShadowFitResult(
            model_family="lightgbm",
            model_hash=hashlib.sha256(model_text.encode()).hexdigest(),
            calibration_predictions=calibration,
            prediction_predictions=prediction,
        )


def _predictions(booster: lgb.Booster, rows: tuple[tuple[float, ...], ...], iteration: int) -> tuple[float, ...]:
    matrix = _NUMPY.asarray(rows, dtype=_NUMPY.float64)
    values = _NUMPY.asarray(booster.predict(matrix, num_iteration=iteration), dtype=_NUMPY.float64).reshape(-1)
    return tuple(float(value) for value in values)


__all__ = ["LightGbmShadowTrainer"]
