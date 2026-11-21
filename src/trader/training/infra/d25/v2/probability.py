"""D25 V2 industry LightGBM fit on the matured D25 hit event."""

from collections.abc import Callable, Sequence

import lightgbm as lgb
import numpy as np

from trader.recommendation.application.ports.loaded_profile import ModelInput
from trader.training.infra.d25.tree import D25TreeModel, DecodeD25Tree, LoadD25TreeScorer
from trader.training.infra.fitting_settings import LightGBMFittingParameters
from trader.training.infra.sample_index import TrainingSampleMatrix

ALGORITHM = "lightgbm_binary"


def FitD25Lightgbm(
    training: TrainingSampleMatrix, parameters: LightGBMFittingParameters, threads: int, check: Callable[[], None],
) -> tuple[D25TreeModel, Callable[[np.ndarray], np.ndarray]]:
    def cancellation_callback(_environment: lgb.callback.CallbackEnv) -> None:
        check()

    tree = lgb.train(
        {"objective": "binary", "metric": "binary_logloss", "learning_rate": parameters.learning_rate,
         "max_depth": parameters.max_depth, "num_leaves": parameters.num_leaves,
         "min_data_in_leaf": parameters.minimum_leaf_rows, "max_bin": parameters.maximum_bins,
         "histogram_pool_size": parameters.histogram_pool_mib, "seed": parameters.seed,
         "deterministic": True, "force_col_wise": True, "num_threads": threads, "verbosity": -1},
        lgb.Dataset(training.features, label=training.labels),
        num_boost_round=parameters.boosting_rounds, callbacks=[cancellation_callback],
    )

    def predict(features: np.ndarray) -> np.ndarray:
        return np.asarray(tree.predict(features, raw_score=True, num_threads=threads))

    return D25TreeModel(tree.model_to_string()), predict


def DecodeD25Classifier(value: object, width: int) -> D25TreeModel:
    return DecodeD25Tree(value, "binary ", width)


def LoadD25ClassifierScorer(model: D25TreeModel, width: int) -> Callable[[Sequence[ModelInput]], np.ndarray]:
    return LoadD25TreeScorer(model, "binary ", width)
