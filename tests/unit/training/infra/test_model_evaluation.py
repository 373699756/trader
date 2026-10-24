"""Past-only calibration, dated evaluation and coverage regression cases."""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pytest

from trader.training.domain.training_dates import BuildTrainingDateSplit
from trader.training.infra.model_bundles.documents import BuildPredictionMetricsDocument
from trader.training.infra.model_evaluation import (
    CalibrationAccumulator,
    CalibrationUnavailableError,
    PredictionMetricsAccumulator,
)
from trader.training.infra.model_fitting import FitIndustryModels, RidgeFittingParameters
from trader.training.infra.profile.v3.contracts import TOMORROW_HEAD_CONTRACT
from trader.training.infra.sample_index import SQLiteTrainingSampleIndex, TrainingSample


@pytest.mark.parametrize(
    "predictions,days,reason",
    (
        ((0.0, 0.0), (date(2026, 1, 1), date(2026, 1, 2)), "variance"),
        ((2.0, 2.0), (date(2026, 1, 1), date(2026, 1, 2)), "variance"),
        ((0.0, 1e-12), (date(2026, 1, 1), date(2026, 1, 2)), "variance"),
        ((0.0, 1.0), (date(2026, 1, 1), date(2026, 1, 1)), "dates"),
        ((0.0, float("nan")), (date(2026, 1, 1), date(2026, 1, 2)), "nonfinite"),
    ),
)
def test_unusable_industry_calibration_has_an_explicit_reason(predictions, days, reason):
    calibration = CalibrationAccumulator()
    with pytest.raises(CalibrationUnavailableError, match=reason):
        calibration.add(np.asarray(predictions), np.asarray([0.0, 1.0]), days)
        calibration.finish()


def test_centered_calibration_matches_across_bounded_batches():
    dates = tuple(date(2026, 1, 1) + timedelta(days=index) for index in range(4))
    predicted = np.asarray([1e6 + index / 10 for index in range(4)])
    target = 0.3 + 1.2 * predicted
    calibration = CalibrationAccumulator()
    calibration.add(predicted[:2], target[:2], dates[:2])
    calibration.add(predicted[2:], target[2:], dates[2:])
    result = calibration.finish()
    assert result.rows == result.dates == 4
    np.testing.assert_allclose(result.intercept + result.slope * predicted, target, rtol=1e-12)
    assert result.prediction_variance > 1e-20


def test_prediction_errors_use_actual_rows_and_rank_ic_weights_each_day_equally():
    values = PredictionMetricsAccumulator()
    first = date(2026, 1, 1)
    for index in range(2):
        values.add(first, f"{index:06d}", "bank", float(index), float(index))
    for index in range(3):
        values.add(first + timedelta(days=1), f"{index:06d}", "energy", float(index), float(2 - index))
    values.add(first + timedelta(days=1), "999999", "uncovered", 7.0, None)
    result = values.finish()
    assert (result.input_rows, result.predicted_rows) == (6, 5)
    assert (result.covered_codes, result.covered_dates, result.covered_industries) == (3, 2, 2)
    assert result.mean_absolute_error == pytest.approx(4 / 5)
    assert result.root_mean_squared_error == pytest.approx(math.sqrt(8 / 5))
    assert result.mean_daily_rank_ic == pytest.approx(0.0)
    assert result.rank_ic_dates == 2


def test_constant_daily_ranks_remain_unavailable_without_a_fabricated_zero():
    values = PredictionMetricsAccumulator()
    for index in range(3):
        values.add(date(2026, 1, 1), f"{index:06d}", "bank", float(index), 1.0)
    result = values.finish()
    assert result.mean_daily_rank_ic is None and result.rank_ic_dates == 0


def test_one_degenerate_industry_does_not_block_other_models_or_count_as_fitted(tmp_path):
    dates = tuple(date(2020, 1, 1) + timedelta(days=index) for index in range(1600))
    split = BuildTrainingDateSplit(dates, parent_manifest_hash="a" * 64)
    with SQLiteTrainingSampleIndex(tmp_path / "samples.sqlite3", 6) as samples:
        for partition in (split.model_fit_dates[:3], split.calibration_dates[:2], split.confirmation_dates[:2]):
            for offset, day in enumerate(partition):
                for industry, prefix in (("usable", 600000), ("degenerate", 600100)):
                    for stock in range(2):
                        value = float(stock + offset / 10)
                        vector = (value,) * 6 if industry == "usable" else (0.0,) * 6
                        samples.add_final(
                            (
                                TrainingSample(
                                    f"{prefix + stock:06d}", day, "main", industry, 1e8, vector, (value * 0.01,) * 6
                                ),
                            )
                        )
        samples.add_final(
            (
                TrainingSample(
                    "600000",
                    split.daily_proxy_holdout_dates[0],
                    "main",
                    "usable",
                    1e8,
                    (999999.0,) * 6,
                    (999999.0,) * 6,
                ),
            )
        )
        samples.prepare_for_model_fitting(split)
        statements: list[str] = []
        samples._connection.observe_statements(statements.append)
        result = FitIndustryModels(samples, split, TOMORROW_HEAD_CONTRACT, RidgeFittingParameters(6, 1.0))
        samples._connection.observe_statements(None)
        assert tuple(item.industry for item in result.models) == ("usable",)
        assert tuple(item.reason for item in result.failures) == ("calibration_prediction_variance_insufficient",)
        assert (result.candidate_training_rows, result.training_rows) == (12, 6)
        assert (result.metrics.input_rows, result.validation_rows) == (8, 4)
        assert result.sealed_holdout_rows == 1
        report = BuildPredictionMetricsDocument(result)
        assert report["prediction_evaluation_mode"] == "prediction_diagnostic_only"
        assert report["evaluation_coverage"]["holdout_status"] == "sealed_rows_count_only"
        assert all("split_name='holdout'" not in statement for statement in statements)
        with pytest.raises(ValueError, match="holdout_is_sealed"):
            tuple(samples.iter_batches("usable", TOMORROW_HEAD_CONTRACT, "holdout"))
