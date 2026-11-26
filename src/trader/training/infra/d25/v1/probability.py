"""D25 V1 grouped LambdaRank on the matured D25 hit event."""

from collections.abc import Callable
from contextlib import ExitStack
from datetime import date
from pathlib import Path

import numpy as np

from trader.recommendation.domain.market.v1.features import LEARNED_RANK_FEATURE_IDS
from trader.recommendation.domain.publication.models import Strategy
from trader.training.domain.v1.learned_rank import LearnedRankDateSplit, V1TrainingSettings
from trader.training.infra.d25.artifact import D25ProbabilityArtifact, D25ProbabilityBinding
from trader.training.infra.d25.calibration import AssessD25Probabilities, CalibrateD25Probability
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
        training_rows, training_positive = len(fit.targets), int(fit.targets.sum())
        fitting_dates = tuple(sorted(set((*fit.dates, *early.dates))))
        calibration_dates = tuple(sorted(set(calibration.dates)))
    sample_rows, labeled_rows = samples.d25_validation_counts(split.validation)
    scores = np.empty(0)
    labels = np.empty(0)
    scored_dates: set[date] = set()
    with ExitStack() as resources:
        if labeled_rows:
            validation, = samples.read_matrices(
                (LearnedRankMatrixRequest(split.validation, Strategy.D25, True),), directory, resources,
                cancel_requested=cancel_requested,
            )
            labels = validation.targets
            scores = np.empty(len(labels))
            for start in range(0, len(labels), 4096):
                CheckV1Cancellation(cancel_requested)
                batch = slice(start, start + 4096)
                scores[batch] = tree.predict(validation.features[batch], raw_score=True, num_threads=compute_threads)
            scored_dates.update(validation.dates)
        assessment = AssessD25Probabilities(
            scores, labels, weights, validation_dates=split.validation, scored_dates=scored_dates,
            sample_rows=sample_rows, labeled_rows=labeled_rows, training_rows=training_rows,
            training_positive_rows=training_positive,
        )
    return D25ProbabilityArtifact(
        "v1", binding, LEARNED_RANK_FEATURE_IDS,
        fitting_dates, calibration_dates, weights, (("", D25TreeModel(tree.model_to_string())),), assessment,
    )
