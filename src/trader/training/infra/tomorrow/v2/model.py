"""Tomorrow V2 full-market LightGBM binary classifier."""

from collections.abc import Callable

import lightgbm as lgb
import numpy as np

from trader.recommendation.domain.market.tomorrow_probability import TOMORROW_FEATURE_NAMES
from trader.training.infra.fitting_settings import LightGBMFittingParameters
from trader.training.infra.tomorrow.tree import LoadTomorrowTree

ALGORITHM = "lightgbm_binary"


def FitTomorrowClassifier(
    features: np.ndarray, labels: np.ndarray, parameters: LightGBMFittingParameters,
    threads: int, check: Callable[[], None],
) -> lgb.Booster:
    def check_iteration(_environment: lgb.callback.CallbackEnv) -> None:
        check()

    return lgb.train(
        {
            "objective": "binary", "learning_rate": parameters.learning_rate,
            "max_depth": parameters.max_depth, "num_leaves": parameters.num_leaves,
            "min_data_in_leaf": parameters.minimum_leaf_rows, "max_bin": parameters.maximum_bins,
            "histogram_pool_size": parameters.histogram_pool_mib, "num_threads": threads, "seed": parameters.seed,
            "deterministic": True, "force_col_wise": True, "verbosity": -1,
        },
        lgb.Dataset(features, label=labels, feature_name=list(TOMORROW_FEATURE_NAMES)),
        num_boost_round=parameters.boosting_rounds,
        callbacks=[check_iteration],
    )


def LoadTomorrowClassifier(tree: str) -> lgb.Booster:
    return LoadTomorrowTree(tree, "binary ")
