"""D25 V1 learned-rank event payload and inference."""

from collections.abc import Callable, Sequence

import numpy as np

from trader.recommendation.application.ports.loaded_profile import ModelInput
from trader.training.infra.d25.tree import D25TreeModel, DecodeD25Tree, LoadD25TreeScorer

ALGORITHM = "lambdarank"


def DecodeD25Ranker(value: object, width: int) -> D25TreeModel:
    return DecodeD25Tree(value, "lambdarank", width)


def LoadD25RankerScorer(model: D25TreeModel, width: int) -> Callable[[Sequence[ModelInput]], np.ndarray]:
    return LoadD25TreeScorer(model, "lambdarank", width)
