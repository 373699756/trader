"""Native LambdaRank fitting for the two event heads and D25 utility."""

from collections.abc import Callable
from dataclasses import dataclass

import lightgbm as lgb
import numpy as np

from trader.training.domain.v1.learned_rank import V1TrainingSettings


def CheckV1Cancellation(cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise InterruptedError("learned_rank_training_cancelled")


def TrainRankDatasets(
    training: lgb.Dataset, validation: lgb.Dataset | None, settings: V1TrainingSettings,
    parameters: dict[str, object], rounds: int, cancel_requested: Callable[[], bool] | None,
) -> lgb.Booster:
    def cancellation_callback(_environment: lgb.callback.CallbackEnv) -> None:
        CheckV1Cancellation(cancel_requested)

    callbacks = [cancellation_callback]
    if validation is not None:
        callbacks.append(lgb.early_stopping(settings.early_stopping_rounds, verbose=False))
    return lgb.train(
        parameters,
        training,
        num_boost_round=rounds,
        valid_sets=[] if validation is None else [validation],
        callbacks=callbacks,
    )


@dataclass(frozen=True, slots=True)
class DailyBinaryRankRows:
    features: np.ndarray
    labels: np.ndarray
    groups: tuple[int, ...]


def FitDailyBinaryRanker(
    fitting: DailyBinaryRankRows, early: DailyBinaryRankRows, settings: V1TrainingSettings,
    *, compute_threads: int, cancel_requested: Callable[[], bool] | None,
    feature_names: tuple[str, ...] | None = None,
) -> lgb.Booster:
    def dataset(rows: DailyBinaryRankRows) -> lgb.Dataset:
        if not rows.groups or sum(rows.groups) != len(rows.labels) or any(size < 1 for size in rows.groups):
            raise ValueError("binary_ranker_groups_invalid")
        return lgb.Dataset(
            rows.features, label=rows.labels,
            weight=DailyRankWeights(len(rows.labels), rows.groups), group=np.asarray(rows.groups, dtype=np.int32),
            feature_name="auto" if feature_names is None else list(feature_names),
        )

    return TrainRankDatasets(
        dataset(fitting), dataset(early), settings, {**RankerParameters(settings, compute_threads), "label_gain": [0, 1]},
        settings.boosting_rounds, cancel_requested,
    )


def RankerParameters(settings: V1TrainingSettings, compute_threads: int) -> dict[str, object]:
    return {
        "objective": "lambdarank",
        "metric": "ndcg",
        "ndcg_eval_at": [6],
        "label_gain": [0, 1, 2, 3, 4],
        "lambdarank_truncation_level": 10,
        "lambdarank_norm": False,
        "learning_rate": settings.learning_rate,
        "max_depth": settings.max_depth,
        "num_leaves": settings.num_leaves,
        "min_data_in_leaf": settings.minimum_leaf_rows,
        "max_bin": settings.maximum_bins,
        "histogram_pool_size": settings.histogram_pool_mib,
        "seed": settings.seed,
        "deterministic": True,
        "force_col_wise": True,
        "num_threads": compute_threads,
        "verbosity": -1,
    }


def DailyRankWeights(samples: int, groups: tuple[int, ...]) -> np.ndarray:
    # Each day has identical total weight; the overall mean row weight is one.
    return np.repeat(np.asarray([samples / (len(groups) * size) for size in groups]), groups)
