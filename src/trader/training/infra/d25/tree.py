"""The native tree payload, loader and scorer shared by the D25 tree heads."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import lightgbm as lgb
import numpy as np

from trader.recommendation.application.ports.loaded_profile import ModelInput
from trader.training.infra.d25.artifact_values import D25ArtifactObject


@dataclass(frozen=True, slots=True)
class D25TreeModel:
    tree: str


def _load_booster(tree: str, objective: str, width: int) -> lgb.Booster:
    try:
        model = lgb.Booster(model_str=tree)
    except lgb.basic.LightGBMError as exc:
        raise ValueError("d25_probability_tree_invalid") from exc
    if model.num_trees() < 1 or model.num_feature() != width or not str(model.dump_model().get("objective", "")).startswith(objective):
        raise ValueError("d25_probability_tree_invalid")
    return model


def DecodeD25Tree(value: object, objective: str, width: int) -> D25TreeModel:
    tree = D25ArtifactObject(value, {"tree"})["tree"]
    if not isinstance(tree, str):
        raise ValueError("d25_probability_tree_invalid")
    _load_booster(tree, objective, width)
    return D25TreeModel(tree)


def LoadD25TreeScorer(
    model: D25TreeModel, objective: str, width: int,
) -> Callable[[Sequence[ModelInput]], np.ndarray]:
    booster = _load_booster(model.tree, objective, width)

    def scores(items: Sequence[ModelInput]) -> np.ndarray:
        values = np.asarray([item.alpha_features for item in items])
        return np.asarray(booster.predict(values, raw_score=True, num_threads=1))

    return scores
