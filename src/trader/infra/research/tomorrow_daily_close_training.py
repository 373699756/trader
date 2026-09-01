"""Fixed, deterministic Ridge and shallow LightGBM implementation for C3."""

from __future__ import annotations

from importlib.metadata import version

import lightgbm as lgb
import numpy as np

from trader.application.research.tomorrow_daily_close_c3 import FittedBaseModels
from trader.application.research.tomorrow_daily_close_training import DailyCloseFeatureRow

_RIDGE_ALPHA = 10.0
_LIGHTGBM_ROUNDS = 80


class FixedC3BaseModelTrainer:
    """Fit the preregistered finite base-model family with bounded resources."""

    def fit(self, training_rows: tuple[DailyCloseFeatureRow, ...], *, feature_count: int) -> FittedBaseModels:
        if (
            not training_rows
            or feature_count < 1
            or any(len(row.feature_values) != feature_count for row in training_rows)
        ):
            raise ValueError("C3 fit requires non-empty rows with the registered feature width")
        features = np.asarray(tuple(row.feature_values for row in training_rows), dtype=np.float64)
        labels = np.asarray(tuple(row.net_excess_returns[0] for row in training_rows), dtype=np.float64)
        means = features.mean(axis=0)
        scales = features.std(axis=0)
        scales = np.where(scales > 1e-12, scales, 1.0)
        normalized = (features - means) / scales
        design = np.column_stack((np.ones(len(normalized)), normalized))
        penalty = np.eye(feature_count + 1, dtype=np.float64) * _RIDGE_ALPHA
        penalty[0, 0] = 0.0
        coefficients = np.linalg.solve(design.T @ design + penalty, design.T @ labels)
        booster = lgb.train(
            {
                "objective": "regression",
                "learning_rate": 0.05,
                "num_leaves": 7,
                "max_depth": 3,
                "min_data_in_leaf": 40,
                "feature_fraction": 1.0,
                "bagging_fraction": 1.0,
                "lambda_l1": 0.0,
                "lambda_l2": 10.0,
                "seed": 20260901,
                "deterministic": True,
                "force_col_wise": True,
                "num_threads": 2,
                "verbosity": -1,
            },
            lgb.Dataset(normalized, label=labels, free_raw_data=True),
            num_boost_round=_LIGHTGBM_ROUNDS,
        )
        return FittedBaseModels(
            preprocessing_means=tuple(float(value) for value in means),
            preprocessing_scales=tuple(float(value) for value in scales),
            ridge_intercept=float(coefficients[0]),
            ridge_coefficients=tuple(float(value) for value in coefficients[1:]),
            lightgbm_model_text=booster.model_to_string(num_iteration=booster.current_iteration()),
            lightgbm_best_iteration=booster.current_iteration(),
            dependency_versions=(("lightgbm", version("lightgbm")), ("numpy", version("numpy"))),
        )

    def predict(
        self,
        fitted: FittedBaseModels,
        rows: tuple[DailyCloseFeatureRow, ...],
    ) -> tuple[tuple[float, ...], tuple[float, ...]]:
        if any(len(row.feature_values) != len(fitted.preprocessing_means) for row in rows):
            raise ValueError("C3 prediction rows do not match the fitted feature width")
        if not rows:
            return (), ()
        features = np.asarray(tuple(row.feature_values for row in rows), dtype=np.float64)
        normalized = (features - np.asarray(fitted.preprocessing_means)) / np.asarray(fitted.preprocessing_scales)
        ridge = fitted.ridge_intercept + normalized @ np.asarray(fitted.ridge_coefficients)
        booster = lgb.Booster(model_str=fitted.lightgbm_model_text)
        lightgbm = booster.predict(normalized, num_iteration=fitted.lightgbm_best_iteration)
        return (
            tuple(float(value) for value in ridge),
            tuple(float(value) for value in lightgbm),
        )


__all__ = ["FixedC3BaseModelTrainer"]
