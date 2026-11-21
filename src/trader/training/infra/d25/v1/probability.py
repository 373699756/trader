"""D25 V1 grouped LambdaRank on the matured D25 hit event."""

from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

import numpy as np

from trader.recommendation.domain.market.v1.features import LEARNED_RANK_FEATURE_IDS
from trader.recommendation.domain.publication.models import Strategy
from trader.training.domain.v1.learned_rank import LearnedRankDateSplit, V1TrainingSettings
from trader.training.infra.d25.artifact import D25ProbabilityArtifact, D25ProbabilityBinding
from trader.training.infra.d25.calibration import CalibrateD25Probability
from trader.training.infra.d25.tree import D25TreeModel
from trader.training.infra.d25.v1.fitting import FitD25LearnedRankClassifier
from trader.training.infra.d25.v1.samples import LearnedRankMatrixRequest, LearnedRankSampleIndex
from trader.training.infra.ranking import CheckV1Cancellation


def FitV1D25Probability(
    samples: LearnedRankSampleIndex,
    directory: Path,
    split: LearnedRankDateSplit,
    settings: V1TrainingSettings,
    binding: D25ProbabilityBinding,
    *,
    compute_threads: int,
    cancel_requested: Callable[[], bool] | None,
) -> D25ProbabilityArtifact:
    with ExitStack() as resources:
        fit, early, calibration = samples.read_matrices(
            tuple(LearnedRankMatrixRequest(days, Strategy.D25, True) for days in (
                split.fitting, split.early_stopping, split.calibration,
            )), directory, resources, cancel_requested=cancel_requested,
        )
        if any(tuple(sorted(set(matrix.dates))) != dates for matrix, dates in (
            (fit, split.fitting), (early, split.early_stopping), (calibration, split.calibration),
        )):
            raise ValueError("d25_probability_v1_dates_incomplete")
        tree = FitD25LearnedRankClassifier(
            fit, early, settings, compute_threads=compute_threads, cancel_requested=cancel_requested,
        )
        scores = np.asarray(tree.predict(calibration.features, raw_score=True, num_threads=compute_threads))
        weights = CalibrateD25Probability(scores, calibration.targets, lambda: CheckV1Cancellation(cancel_requested))
    return D25ProbabilityArtifact(
        "v1", binding, LEARNED_RANK_FEATURE_IDS,
        tuple(sorted(set((*fit.dates, *early.dates)))), tuple(sorted(set(calibration.dates))),
        weights, (("", D25TreeModel(tree.model_to_string())),),
    )
