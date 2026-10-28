from __future__ import annotations

from contextlib import ExitStack
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from trader.training.application.tomorrow_training import TOMORROW_TRAINING_COMPUTE_THREADS
from trader.training.domain.training_dates import BuildTrainingDateSplit
from trader.training.infra import model_fitting
from trader.training.infra.model_bundles.bundle_codec import TrainedRidgeModelSnapshot
from trader.training.infra.model_bundles.contracts import TrainedHeadContract
from trader.training.infra.model_evaluation import CalibrationResult, PredictionMetricsAccumulator
from trader.training.infra.model_fitting import FitIndustryModels
from trader.training.infra.model_fitting_results import FittedIndustryModel
from trader.training.infra.profile.v2.contracts import V2_HEAD_CONTRACTS, V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import HEAD_CONTRACTS, V3_TRAINING_PROFILE
from trader.training.infra.sample_cache import TrainingSampleCache
from trader.training.infra.sample_index import TrainingSample, TrainingSampleBatch
from trader.training.infra.training_settings import LoadProfileTrainingSettings

PROJECT_ROOT = Path(__file__).resolve().parents[6]


@pytest.mark.parametrize("contract", (*V2_HEAD_CONTRACTS, *HEAD_CONTRACTS), ids=lambda item: item.model_id)
def test_industry_fitting_matches_after_complete_cache_reopen(
    tmp_path: Path, monkeypatch, contract: TrainedHeadContract
) -> None:
    profile = V2_TRAINING_PROFILE if contract in V2_HEAD_CONTRACTS else V3_TRAINING_PROFILE
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1_500))
    split = BuildTrainingDateSplit(dates, parent_manifest_hash="a" * 64, history_sessions=profile.history_sessions)
    usable_dates = tuple((*split.development_dates, *split.confirmation_dates, *split.daily_proxy_holdout_dates))
    settings = LoadProfileTrainingSettings(PROJECT_ROOT, profile.profile_id)

    feature_count = max(contract.feature_positions) + 1
    cache = TrainingSampleCache(tmp_path, contract.model_id, profile.heads[0].feature_manifest.names, split.content_hash)
    with cache.open() as repository, ExitStack() as resources:
        for day_position, day in enumerate(usable_dates):
            repository.add_final(_samples_for_day(day, day_position, feature_count))
        repository.prepare_for_model_fitting(split)
        counts = repository.industry_counts(contract)[0]
        data = resources.enter_context(repository.industry_data(counts, contract))
        means = data.training.features.mean(axis=0)
        deviations = data.training.features.std(axis=0)
        scales = np.where(deviations > 1e-12, deviations, 1.0)
        expected_training = (data.training.features - means) / scales
        expected_early = (data.early_stopping.features - means) / scales
        reference_fit = model_fitting._fit_lightgbm

        def checked_fit(training, early, parameters, heartbeat):
            assert training.features.dtype == early.features.dtype == np.float64
            np.testing.assert_array_equal(training.features, expected_training)
            np.testing.assert_array_equal(early.features, expected_early)
            return reference_fit(training, early, parameters, heartbeat)

        monkeypatch.setattr(model_fitting, "_fit_lightgbm", checked_fit)

        first = FitIndustryModels(repository, split, contract, settings.fitting)
        cache.seal(repository)

    with cache.open() as repository, ExitStack() as resources:
        assert cache.reused
        second = FitIndustryModels(repository, split, contract, settings.fitting)

    assert TOMORROW_TRAINING_COMPUTE_THREADS == 2
    assert first == second
    assert first.training_rows == second.training_rows
    assert first.validation_rows == second.validation_rows
    fitted = next(item.model for item in first.models if item.industry == "银行")
    if profile.algorithm == "lightgbm":
        assert "[num_threads: 2]" in fitted.lightgbm_model
        assert "[force_col_wise: 1]" in fitted.lightgbm_model
    else:
        assert len(fitted.ridge_coefficients) == len(contract.feature_positions)


def _samples_for_day(day: date, day_position: int, feature_count: int) -> tuple[TrainingSample, ...]:
    return tuple(
        TrainingSample(
            f"{code:06d}",
            day,
            "main",
            "银行",
            1_000_000.0 + code,
            tuple((day_position % (offset + 11) + code % (offset + 5)) / 100.0 for offset in range(feature_count)),
            (((day_position % 17) - (code % 7)) / 1_000.0,) * 6,
        )
        for code in range(1, 36)
    )


def test_validation_keeps_sorted_industry_calls_batch_membership_and_metric_row_order(monkeypatch):
    first, second = date(2026, 1, 5), date(2026, 1, 6)
    batches = tuple(
        TrainingSampleBatch(
            np.asarray([[value, value * 2, 0.0, 0.0, 0.0, 0.0] for value in values]),
            np.asarray(labels),
            (day,) * len(values),
            tuple(f"{number:06d}" for number in range(len(values))),
            industries,
        )
        for day, values, labels, industries in (
            (
                first,
                (20.0, 30.0, 10.0, 40.0, 50.0),
                (0.1, 0.2, 0.3, 0.4, 0.5),
                ("beta", "missing", "alpha", "beta", "alpha"),
            ),
            (second, (60.0, 70.0, 80.0), (0.6, 0.7, 0.8), ("beta", "alpha", "missing")),
        )
    )
    models = tuple(
        FittedIndustryModel(
            industry,
            TrainedRidgeModelSnapshot((0.0,) * 6, (1.0,) * 6, 3.0, 0.5, intercept, coefficients),
            10,
            CalibrationResult(2, 2, 1.0, 3.0, 0.5),
        )
        for industry, intercept, coefficients in (
            ("beta", 2.0, (0.0, 1.0, 0.0, 0.0, 0.0, 0.0)),
            ("alpha", 1.0, (1.0, 0.0, 0.0, 0.0, 0.0, 0.0)),
        )
    )

    class Samples:
        def iter_validation_batches(self, contract):
            assert contract == HEAD_CONTRACTS[0]
            yield from batches

    calls = []
    predict = model_fitting._ValidationPredictor.predict

    def observed(predictor, features):
        calls.append((predictor._model.ridge_intercept, features.copy()))
        return predict(predictor, features)

    monkeypatch.setattr(model_fitting._ValidationPredictor, "predict", observed)
    heartbeats = []
    actual = model_fitting._evaluate_validation(Samples(), HEAD_CONTRACTS[0], models, lambda: heartbeats.append(True))

    assert len(heartbeats) == 2
    assert [intercept for intercept, _ in calls] == [1.0, 2.0, 1.0, 2.0]
    for (_, features), expected in zip(calls, ((10.0, 50.0), (20.0, 40.0), (70.0,), (60.0,)), strict=True):
        np.testing.assert_array_equal(features[:, 0], expected)
        assert features.shape == (len(expected), 6)
    overall = PredictionMetricsAccumulator()
    industries = {industry: PredictionMetricsAccumulator() for industry in ("alpha", "beta")}
    for batch, predictions in zip(batches, ((24.0, None, 8.5, 44.0, 28.5), (64.0, 38.5, None)), strict=True):
        for day, code, industry, target, prediction in zip(
            batch.trade_dates, batch.codes, batch.industries, batch.labels, predictions, strict=True
        ):
            overall.add(day, code, industry, float(target), prediction)
            if industry in industries:
                industries[industry].add(day, code, industry, float(target), prediction)
    assert actual == (overall.finish(), tuple((industry, value.finish()) for industry, value in industries.items()))
