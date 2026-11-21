"""Tomorrow V1 full-market grouped ranking."""

from collections.abc import Callable

import lightgbm as lgb
import numpy as np

from trader.recommendation.domain.market.tomorrow_probability import TOMORROW_FEATURE_NAMES
from trader.training.domain.v1.learned_rank import V1TrainingSettings
from trader.training.infra.ranking import DailyBinaryRankRows, FitDailyBinaryRanker
from trader.training.infra.tomorrow.tree import LoadTomorrowTree

ALGORITHM = "lambdarank"


def FitTomorrowRanker(
    fitting: DailyBinaryRankRows, early: DailyBinaryRankRows, settings: V1TrainingSettings,
    check: Callable[[], None], threads: int,
) -> lgb.Booster:
    if len(fitting.labels) < settings.minimum_training_rows:
        raise ValueError("tomorrow_ranker_training_rows_insufficient")
    if not np.any(fitting.labels > 0) or not np.any(fitting.labels == 0):
        raise ValueError("tomorrow_partition_requires_both_labels")

    def cancelled() -> bool:
        check()
        return False

    return FitDailyBinaryRanker(
        fitting, early, settings, compute_threads=threads, cancel_requested=cancelled,
        feature_names=TOMORROW_FEATURE_NAMES,
    )


def LoadTomorrowRanker(tree: str) -> lgb.Booster:
    return LoadTomorrowTree(tree, "lambdarank")
