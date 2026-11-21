"""Native tree decoding and raw scores for Tomorrow's two tree heads."""

import lightgbm as lgb
import numpy as np

from trader.recommendation.domain.market.tomorrow_probability import TOMORROW_FEATURE_NAMES


def LoadTomorrowTree(tree: str, objective: str) -> lgb.Booster:
    try:
        booster = lgb.Booster(model_str=tree)
    except lgb.basic.LightGBMError as exc:
        raise ValueError("tomorrow_probability_artifact_tree_invalid") from exc
    if (
        booster.num_trees() < 1 or tuple(booster.feature_name()) != TOMORROW_FEATURE_NAMES
        or not str(booster.dump_model().get("objective", "")).startswith(objective)
    ):
        raise ValueError("tomorrow_probability_artifact_tree_invalid")
    return booster


def TomorrowTreeScores(model: lgb.Booster, values: np.ndarray) -> np.ndarray:
    return np.asarray(model.predict(values, raw_score=True, num_threads=1), dtype=np.float64).reshape(-1)
