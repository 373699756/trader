from datetime import date, timedelta

from trader.application.research.tomorrow_daily_close_c3 import (
    C3CandidateEvaluator,
    C3CandidateTrainer,
    FittedBaseModels,
)
from trader.application.research.tomorrow_daily_close_training import DailyCloseSourceSample, build_feature_dataset
from trader.domain.research.tomorrow_daily_close import split_complete_trading_dates

_HASH_A = "a" * 64
_HASH_B = "b" * 64


class _FitPort:
    def fit(self, training_rows, *, feature_count):
        mean = sum(row.net_excess_returns[0] for row in training_rows) / len(training_rows)
        return FittedBaseModels(
            preprocessing_means=(0.0,) * feature_count,
            preprocessing_scales=(1.0,) * feature_count,
            ridge_intercept=mean,
            ridge_coefficients=(0.001,) * feature_count,
            lightgbm_model_text="tree\nend",
            lightgbm_best_iteration=1,
            dependency_versions=(("test_fit", "1"),),
        )

    def predict(self, fitted, rows):
        ridge = tuple(fitted.ridge_intercept + sum(row.feature_values) * 0.001 for row in rows)
        lightgbm = tuple(value + 0.0002 for value in ridge)
        return ridge, lightgbm


def _dataset():
    first = date(2020, 1, 1)
    dates = tuple(first + timedelta(days=index) for index in range(320))
    samples = tuple(
        DailyCloseSourceSample(
            trade_date=day,
            label_maturity_date=day + timedelta(days=1),
            code=f"60{code_index:04d}",
            board=("main", "chinext", "star")[code_index % 3],
            feature_values=(float(index % 11), float(code_index)),
            net_excess_returns=(0.003 + code_index * 0.0001, 0.0001, -0.0049),
            hard_filter_passed=True,
            hard_filter_evidence_complete=True,
            filter_evidence_hash=_HASH_A,
            source_row_hash=(f"{index * 10 + code_index:064x}")[-64:],
        )
        for index, day in enumerate(dates)
        for code_index in range(3)
    )
    return build_feature_dataset(
        samples,
        feature_names=("qfq_return_1d", "qfq_return_3d"),
        feature_units=("ratio", "ratio"),
        source_archive_hash=_HASH_A,
        filter_spec_hash=_HASH_B,
    )


def test_c3_development_builds_fixed_five_candidate_oof_without_reserved_dates() -> None:
    dataset = _dataset()
    split = split_complete_trading_dates(dataset.manifest.trading_dates)

    result = C3CandidateTrainer(_FitPort(), C3CandidateEvaluator()).develop(dataset, split)

    assert tuple(item.candidate_id for item in result.candidates) == (
        "ridge",
        "lightgbm",
        "ridge_lightgbm_ensemble",
        "ensemble_stratum_residual",
        "ensemble_stratum_stock_residual",
    )
    assert all(item.predictions for item in result.candidates)
    assert all(
        prediction.trade_date in split.development_dates
        for candidate in result.candidates
        for prediction in candidate.predictions
    )
    assert not set(split.point_in_time_reserved_dates).intersection(
        prediction.trade_date for candidate in result.candidates for prediction in candidate.predictions
    )
    assert result.selected_candidate_id in {item.candidate_id for item in result.candidates}
    assert result.terminal_holdout_opened is False
    assert result.production_authority is False


def test_c3_freeze_refits_selected_pipeline_without_opening_reserved_dates() -> None:
    dataset = _dataset()
    split = split_complete_trading_dates(dataset.manifest.trading_dates)
    trainer = C3CandidateTrainer(_FitPort(), C3CandidateEvaluator())
    development = trainer.develop(dataset, split)

    artifact = trainer.freeze(dataset, split, development, confirmation_report_hash=_HASH_A)

    assert artifact.candidate_id == development.selected_candidate_id
    assert artifact.trained_through < split.terminal_holdout_dates[0]
    assert artifact.production_authority is False
    assert artifact.automatic_model_update is False
