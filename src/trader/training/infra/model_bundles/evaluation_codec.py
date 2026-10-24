"""Strict diagnostic-report schema; no synthetic metrics for old model bundles."""

from __future__ import annotations

from typing import cast

from trader.infra.serialization.fields import IsFiniteNumber, IsInteger
from trader.training.infra.model_evaluation import (
    MINIMUM_CALIBRATION_VARIANCE,
    PREDICTION_EVALUATION_CONTRACT,
)

PREDICTION_REPORT_FIELDS = {
    "evaluation_contract",
    "prediction_evaluation_mode",
    "prediction_metrics",
    "industry_prediction_metrics",
    "calibration_metrics",
    "rejected_industries",
    "evaluation_coverage",
}
_METRIC_FIELDS = {
    "input_rows",
    "predicted_rows",
    "covered_codes",
    "covered_dates",
    "covered_industries",
    "mean_absolute_error",
    "root_mean_squared_error",
    "mean_daily_rank_ic",
    "rank_ic_dates",
}
_FAILURE_REASONS = {
    "industry_training_rows_insufficient",
    "calibration_samples_or_dates_insufficient",
    "industry_early_stopping_rows_insufficient",
    "industry_validation_rows_insufficient",
    "calibration_nonfinite_or_invalid",
    "calibration_prediction_variance_insufficient",
}
_RANK_IC_ABSOLUTE_LIMIT = 1.000001


def ValidatePredictionReport(payload: dict[str, object]) -> None:
    if (
        payload["evaluation_contract"] != PREDICTION_EVALUATION_CONTRACT
        or payload["prediction_evaluation_mode"] != "prediction_diagnostic_only"
    ):
        raise ValueError("trained-head prediction evaluation contract is invalid")
    overall = _metrics(payload["prediction_metrics"])
    industries, predicted_rows = _validated_industries(
        payload["industry_prediction_metrics"], payload["calibration_metrics"]
    )
    if (
        predicted_rows != overall["predicted_rows"]
        or overall["covered_industries"] != len(industries)
        or payload["validation_rows"] != predicted_rows
        or payload["industry_count"] != len(industries)
    ):
        raise ValueError("trained-head actual prediction totals are invalid")
    coverage = _validated_coverage(payload["evaluation_coverage"], payload, overall, predicted_rows)
    rejected_training, rejected_validation = _rejected_coverage(payload["rejected_industries"], industries)
    if rejected_training + _count(coverage, "trained_rows") != _count(
        coverage, "candidate_training_rows"
    ) or rejected_validation + predicted_rows != _count(coverage, "candidate_validation_rows"):
        raise ValueError("trained-head rejected and fitted row coverage is inconsistent")


def _validated_industries(raw_metrics: object, raw_calibrations: object) -> tuple[dict[str, object], int]:
    industries = _object(raw_metrics)
    calibrations = _object(raw_calibrations)
    if not industries or set(industries) != set(calibrations):
        raise ValueError("trained-head prediction industry coverage is invalid")
    predicted_rows = 0
    for industry, raw in industries.items():
        if not industry.strip():
            raise ValueError("trained-head prediction industry identity is invalid")
        metric = _metrics(raw)
        if metric["covered_industries"] != 1:
            raise ValueError("trained-head per-industry prediction coverage is invalid")
        predicted_rows += _count(metric, "predicted_rows", minimum=1)
        _validate_calibration(calibrations[industry])
    return industries, predicted_rows


def _validate_calibration(raw: object) -> None:
    calibration = _object(raw)
    if set(calibration) != {"rows", "dates", "prediction_variance", "intercept", "slope"}:
        raise ValueError("trained-head calibration fields are invalid")
    rows, days = _count(calibration, "rows", minimum=2), _count(calibration, "dates", minimum=2)
    if days > rows:
        raise ValueError("trained-head calibration dates are invalid")
    for name in ("prediction_variance", "intercept", "slope"):
        if not IsFiniteNumber(calibration[name]):
            raise ValueError("trained-head calibration values are invalid")
    variance = calibration["prediction_variance"]
    if not IsFiniteNumber(variance) or variance <= MINIMUM_CALIBRATION_VARIANCE:
        raise ValueError("trained-head calibration variance is invalid")


def _validated_coverage(
    raw: object, payload: dict[str, object], overall: dict[str, object], predicted_rows: int
) -> dict[str, object]:
    coverage = _object(raw)
    if set(coverage) != {
        "candidate_training_rows",
        "trained_rows",
        "candidate_validation_rows",
        "predicted_rows",
        "sealed_holdout_rows",
        "holdout_status",
    }:
        raise ValueError("trained-head evaluation coverage fields are invalid")
    for name in (
        "candidate_training_rows",
        "trained_rows",
        "candidate_validation_rows",
        "predicted_rows",
        "sealed_holdout_rows",
    ):
        _count(coverage, name)
    if (
        coverage["holdout_status"] != "sealed_rows_count_only"
        or coverage["trained_rows"] != payload["training_rows"]
        or coverage["predicted_rows"] != predicted_rows
        or coverage["candidate_validation_rows"] != overall["input_rows"]
        or _count(coverage, "trained_rows") > _count(coverage, "candidate_training_rows")
    ):
        raise ValueError("trained-head evaluation coverage is inconsistent")
    return coverage


def _rejected_coverage(raw: object, industries: dict[str, object]) -> tuple[int, int]:
    rejected = raw
    if not isinstance(rejected, list):
        raise ValueError("trained-head rejected industries must be a list")
    failed: set[str] = set()
    rejected_training = 0
    rejected_validation = 0
    for value in rejected:
        item = _object(value)
        if set(item) != {
            "industry",
            "reason",
            "training_rows",
            "calibration_rows",
            "calibration_dates",
            "validation_rows",
        }:
            raise ValueError("trained-head rejected industry fields are invalid")
        failed_industry = item["industry"]
        if (
            not isinstance(failed_industry, str)
            or not failed_industry.strip()
            or failed_industry in failed
            or failed_industry in industries
        ):
            raise ValueError("trained-head rejected industry identity is invalid")
        if not isinstance(item["reason"], str) or item["reason"] not in _FAILURE_REASONS:
            raise ValueError("trained-head rejected industry reason is invalid")
        failed.add(failed_industry)
        for name in ("training_rows", "calibration_rows", "calibration_dates", "validation_rows"):
            _count(item, name)
        rejected_training += _count(item, "training_rows")
        rejected_validation += _count(item, "validation_rows")
    return rejected_training, rejected_validation


def ValidatePredictionSampleCounts(payload: dict[str, object], counts: dict[str, object]) -> None:
    metrics = _object(payload["industry_prediction_metrics"])
    if set(metrics) != set(counts):
        raise ValueError("trained-head predicted industries differ from model industries")
    training = 0
    validation = 0
    for industry, raw in counts.items():
        item = _object(raw)
        training += _count(item, "training_rows", minimum=1)
        actual = _count(item, "validation_rows", minimum=1)
        if actual != _object(metrics[industry])["predicted_rows"]:
            raise ValueError("trained-head predicted sample count is inconsistent")
        validation += actual
    if training != payload["training_rows"] or validation != payload["validation_rows"]:
        raise ValueError("trained-head fitted sample totals are inconsistent")


def _metrics(raw: object) -> dict[str, object]:
    value = _object(raw)
    if set(value) != _METRIC_FIELDS:
        raise ValueError("trained-head prediction metric fields are invalid")
    for name in (
        "input_rows",
        "predicted_rows",
        "covered_codes",
        "covered_dates",
        "covered_industries",
        "rank_ic_dates",
    ):
        _count(value, name)
    predicted = _count(value, "predicted_rows", minimum=1)
    if (
        _count(value, "input_rows") < predicted
        or any(
            not 1 <= _count(value, name) <= predicted
            for name in ("covered_codes", "covered_dates", "covered_industries")
        )
        or _count(value, "rank_ic_dates") > _count(value, "covered_dates")
    ):
        raise ValueError("trained-head prediction metric coverage is invalid")
    for name in ("mean_absolute_error", "root_mean_squared_error"):
        number = value[name]
        if not IsFiniteNumber(number) or number < 0:
            raise ValueError("trained-head prediction error is invalid")
    rank_ic = value["mean_daily_rank_ic"]
    if value["rank_ic_dates"] == 0:
        if rank_ic is not None:
            raise ValueError("trained-head unavailable rank IC must remain null")
    elif not IsFiniteNumber(rank_ic) or not -_RANK_IC_ABSOLUTE_LIMIT <= rank_ic <= _RANK_IC_ABSOLUTE_LIMIT:
        raise ValueError("trained-head daily rank IC is invalid")
    return value


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("trained-head evaluation value must be an object")
    return cast(dict[str, object], value)


def _count(value: dict[str, object], name: str, *, minimum: int = 0) -> int:
    count = value[name]
    if not IsInteger(count) or count < minimum:
        raise ValueError("trained-head evaluation count is invalid")
    return count


__all__ = ["PREDICTION_REPORT_FIELDS", "ValidatePredictionReport", "ValidatePredictionSampleCounts"]
