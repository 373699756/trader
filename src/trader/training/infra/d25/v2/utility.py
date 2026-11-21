"""D25 V2 LightGBM regression kernel for independent utility estimation."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import lightgbm as lgb

if TYPE_CHECKING:
    from trader.training.infra.fitting_settings import LightGBMFittingParameters
    from trader.training.infra.sample_index import TrainingSampleMatrix


def FitD25UtilityTree(
    training: TrainingSampleMatrix,
    early: TrainingSampleMatrix | None,
    parameters: LightGBMFittingParameters,
    heartbeat: Callable[[], None],
    compute_threads: int,
) -> lgb.Booster:
    booster = lgb.train(
        {
            "objective": "regression_l2",
            "learning_rate": parameters.learning_rate,
            "max_depth": parameters.max_depth,
            "num_leaves": parameters.num_leaves,
            "min_data_in_leaf": parameters.minimum_leaf_rows,
            "num_boost_round": parameters.boosting_rounds,
            "max_bin": parameters.maximum_bins,
            "deterministic": True,
            "seed": parameters.seed,
            "feature_fraction_seed": parameters.seed,
            "bagging_seed": parameters.seed,
            "data_random_seed": parameters.seed,
            "num_threads": compute_threads,
            "force_col_wise": True,
            "histogram_pool_size": parameters.histogram_pool_mib,
            "verbosity": -1,
        },
        lgb.Dataset(training.features, label=training.labels),
        num_boost_round=parameters.boosting_rounds,
        valid_sets=[lgb.Dataset(early.features, label=early.labels)] if early is not None else None,
        callbacks=[*( [lgb.early_stopping(parameters.early_stopping_rounds, verbose=False)] if early is not None else []),
                   lambda _env: heartbeat()],
    )
    booster.free_dataset()
    return booster
