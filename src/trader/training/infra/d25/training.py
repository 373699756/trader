"""Fit threshold-specific D25 models and calibrate fixed weights out of sample."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from functools import partial
import numpy as np

from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.domain.publication.models import Strategy
from trader.training.application.training_progress import (
    TrainingProgress, TrainingProgressState, TrainingFitCounts, TrainingFitDetail,
)
from trader.training.domain.production_diagnostic_dates import ProductionDiagnosticDatePlan
from trader.training.infra.d25.artifact import (
    D25Model,
    D25ProbabilityArtifact,
    D25ProbabilityBinding,
)
from trader.training.infra.d25.calibration import AssessD25Probabilities, CalibrateD25Probability, HasD25ProbabilityLabels
from trader.training.infra.d25.v2.probability import FitD25Lightgbm
from trader.training.infra.d25.v3.probability import FitD25Logistic
from trader.training.infra.d25.v5.probability import FitD25Tcn
from trader.training.infra.model_bundles.contracts import TrainedHeadContract
from trader.training.infra.fitting_settings import LightGBMFittingParameters, ModelFittingParameters
from trader.training.infra.tcn.settings import TcnFittingParameters
from trader.training.infra.sample_index import SQLiteTrainingSampleIndex, TrainingSampleMatrix


@dataclass(frozen=True, slots=True)
class D25IndustryFitRequest:
    profile: ScoringProfileId
    contract: TrainedHeadContract
    split: ProductionDiagnosticDatePlan
    parameters: ModelFittingParameters
    industries: tuple[str, ...]
    compute_threads: int
    binding: D25ProbabilityBinding


def FitIndustryD25Probability(
    samples: SQLiteTrainingSampleIndex,
    request: D25IndustryFitRequest,
    check: Callable[[], None],
    *,
    progress: Callable[[TrainingProgress], None] | None = None,
) -> D25ProbabilityArtifact:
    models: list[tuple[str, D25Model]] = []
    fitted_dates: set[date] = set()
    calibration_dates: set[date] = set()
    selected = frozenset(request.industries)
    counts = tuple(item for item in samples.industry_counts(request.contract, d25_probability=True) if item.industry in selected)
    scores = np.empty(sum(item.calibration for item in counts), dtype=np.float64)
    labels = np.empty_like(scores)
    validation_scores = np.empty(sum(item.validation for item in counts))
    validation_labels = np.empty_like(validation_scores)
    validation_dates: set[date] = set()
    validation_position = training_rows = training_positive_rows = 0
    position = skipped = 0

    def report(state: TrainingProgressState, detail: TrainingFitDetail | None = None) -> None:
        if progress is not None:
            fitted = len(models)
            progress(TrainingProgress(
                "event_model_fit", state, fitted + skipped, len(counts), fitted, Strategy.D25,
                fit_detail=detail, count_unit="industries", fit_counts=TrainingFitCounts(fitted + skipped, len(counts), fitted, skipped),
            ))

    report("started")
    def report_epoch(industry: str, rows: int, epochs: int, epoch: int, completed: int, total: int) -> None:
        report("running", TrainingFitDetail(industry, rows, "training", epoch, epochs, completed, total))

    for count in counts:
        check()
        industry = count.industry
        epochs = request.parameters.epochs if isinstance(request.parameters, TcnFittingParameters) else 0
        detail = TrainingFitDetail(industry, count.training, "preparation", total_epochs=epochs)
        report("running", detail)
        model_result = None
        if count.training >= request.parameters.minimum_industry_training_rows:
            with samples.d25_probability_training_data(count, request.contract, check) as (training, dates):
                if HasD25ProbabilityLabels(training.labels):
                    model_result = _fit_industry(
                        training, request.parameters, request.compute_threads, check,
                        partial(report_epoch, industry, count.training, epochs),
                    )
                    training_rows += len(training.labels)
                    training_positive_rows += int(training.labels.sum())
        if model_result is None:
            skipped += 1
            report("running", TrainingFitDetail(
                industry, count.training, "skipped", failure_reason="d25_probability_industry_labels_insufficient",
            ))
            continue
        model, logits = model_result
        calibrated_rows = 0
        for batch in samples.iter_batches(industry, request.contract, "calibration", d25_probability=True):
            check()
            end = position + len(batch.labels)
            if end > len(scores):
                raise ValueError("d25_probability_calibration_counts_changed")
            scores[position:end] = logits(batch.features)
            labels[position:end] = batch.labels
            calibration_dates.update(batch.trade_dates)
            calibrated_rows += len(batch.labels)
            position = end
        if calibrated_rows != count.calibration:
            raise ValueError("d25_probability_calibration_counts_changed")
        # Freeze held-out raw predictions while this industry's native scorer is resident.
        # Calibration below reads only the separate calibration partition.
        start = validation_position
        for batch in samples.iter_batches(industry, request.contract, "validation", d25_probability=True):
            check()
            end = validation_position + len(batch.labels)
            validation_scores[validation_position:end] = logits(batch.features)
            validation_labels[validation_position:end] = batch.labels
            validation_dates.update(batch.trade_dates)
            validation_position = end
        if validation_position - start != count.validation:
            raise ValueError("d25_probability_validation_counts_changed")
        models.append((industry, model))
        fitted_dates.update(dates)
        report("running", TrainingFitDetail(industry, count.training, "completed"))
    report("completed")
    if not models or not position:
        raise ValueError("d25_probability_industry_labels_insufficient")
    if tuple(sorted(fitted_dates)) != request.split.fitting_dates or tuple(sorted(calibration_dates)) != request.split.calibration_dates:
        raise ValueError("d25_probability_dates_incomplete")
    if progress is not None:
        progress(TrainingProgress("model_calibration", "started", 0, 1, strategy=Strategy.D25))
    try:
        weights = CalibrateD25Probability(scores[:position], labels[:position], check)
    except ValueError:
        if progress is not None:
            progress(TrainingProgress("model_calibration", "failed", 0, 1, strategy=Strategy.D25))
        raise
    if progress is not None:
        progress(TrainingProgress("model_calibration", "completed", 1, 1, strategy=Strategy.D25))
    sample_rows, labeled_rows = samples.d25_validation_counts()
    assessment = AssessD25Probabilities(
        validation_scores[:validation_position], validation_labels[:validation_position], weights,
        validation_dates=request.split.validation_dates, scored_dates=validation_dates, sample_rows=sample_rows,
        labeled_rows=labeled_rows, training_rows=training_rows, training_positive_rows=training_positive_rows,
    )
    return D25ProbabilityArtifact(
        request.profile, request.binding, request.contract.feature_manifest.names,
        tuple(sorted(fitted_dates)), tuple(sorted(calibration_dates)),
        weights, tuple(models), assessment,
    )


def _fit_industry(
    training: TrainingSampleMatrix, parameters: ModelFittingParameters, threads: int, check: Callable[[], None],
    report: Callable[[int, int, int], None],
) -> tuple[D25Model, Callable[[np.ndarray], np.ndarray]]:
    if isinstance(parameters, LightGBMFittingParameters):
        return FitD25Lightgbm(training, parameters, threads, check)
    if isinstance(parameters, TcnFittingParameters):
        return FitD25Tcn(training, parameters, threads, check, report=report)
    return FitD25Logistic(training, parameters, check)
