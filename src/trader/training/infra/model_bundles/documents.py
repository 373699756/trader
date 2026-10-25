"""Whitelisted model and diagnostic documents at the persistence boundary."""

from dataclasses import asdict

from trader.recommendation.domain.market.v5.features import SEQUENCE_CONTRACT
from trader.training.infra.model_bundles.bundle_codec import (
    TrainedLightGBMModelSnapshot,
    TrainedRidgeModelSnapshot,
    TrainedTcnModelSnapshot,
)
from trader.training.infra.model_evaluation import PREDICTION_EVALUATION_CONTRACT
from trader.training.infra.model_fitting_results import FittedIndustryModel, IndustryFitResult
from trader.training.infra.profile.v5.settings import TCN_NETWORK_CONTRACT
from trader.training.infra.sample_index import TargetMetric


def BuildTargetMetricDocument(metric: TargetMetric) -> dict[str, object]:
    return {"count": metric.count, "mean": metric.mean, "standard_deviation": metric.standard_deviation}


def BuildIndustryModelDocument(result: IndustryFitResult) -> dict[str, object]:
    return {"industries": {item.industry: _model_document(item) for item in result.models}}


def _model_document(item: FittedIndustryModel) -> dict[str, object]:
    model = item.model
    document: dict[str, object] = {
        "transformer_means": list(model.transformer_means),
        "transformer_scales": list(model.transformer_scales),
        "calibration_intercept": model.calibration_intercept,
        "calibration_slope": model.calibration_slope,
    }
    if isinstance(model, TrainedLightGBMModelSnapshot):
        document.update(lightgbm_model=model.lightgbm_model, lightgbm_best_iteration=model.lightgbm_best_iteration)
    elif isinstance(model, TrainedRidgeModelSnapshot):
        document.update(ridge_intercept=model.ridge_intercept, ridge_coefficients=list(model.ridge_coefficients))
    elif isinstance(model, TrainedTcnModelSnapshot):
        document["tcn_network"] = {
            "contract": SEQUENCE_CONTRACT,
            "architecture": TCN_NETWORK_CONTRACT,
            "hidden_channels": model.architecture.hidden_channels,
            "compute_dtype": model.architecture.compute_dtype,
            "dropout": model.dropout,
            "weights": [{"name": t.name, "shape": list(t.shape), "values": list(t.values)} for t in model.weights],
        }
    return document


def BuildPredictionMetricsDocument(result: IndustryFitResult) -> dict[str, object]:
    return {
        "evaluation_contract": PREDICTION_EVALUATION_CONTRACT,
        "prediction_evaluation_mode": "prediction_diagnostic_only",
        "prediction_metrics": asdict(result.metrics),
        "industry_prediction_metrics": {industry: asdict(metrics) for industry, metrics in result.industry_metrics},
        "calibration_metrics": {item.industry: asdict(item.calibration) for item in result.models},
        "rejected_industries": [asdict(item) for item in result.failures],
        "evaluation_coverage": {
            "candidate_training_rows": result.candidate_training_rows,
            "trained_rows": result.training_rows,
            "candidate_validation_rows": result.metrics.input_rows,
            "predicted_rows": result.validation_rows,
            "sealed_holdout_rows": result.sealed_holdout_rows,
            "holdout_status": "sealed_rows_count_only",
        },
    }


def BuildIndustrySampleCountsDocument(result: IndustryFitResult) -> dict[str, object]:
    metrics = dict(result.industry_metrics)
    return {
        item.industry: {
            "training_rows": item.training_rows,
            "validation_rows": metrics[item.industry].predicted_rows,
        }
        for item in result.models
    }


__all__ = [
    "BuildIndustryModelDocument",
    "BuildPredictionMetricsDocument",
    "BuildIndustrySampleCountsDocument",
    "BuildTargetMetricDocument",
]
