from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from trader.application.research.tomorrow_daily_close_training import (
    CandidateModelArtifact,
    DailyCloseSourceSample,
    ModelDependencyVersion,
    StockResidualCorrection,
    StratumCorrection,
    ValidationMetrics,
    ValidationReport,
    build_feature_dataset,
    select_mature_fold_training_rows,
    select_mature_training_rows,
)
from trader.domain.research.tomorrow_daily_close import build_expanding_walk_forward, split_complete_trading_dates

_HASH_A = "a" * 64
_HASH_B = "b" * 64
_FEATURES = ("qfq_return_1d", "qfq_return_3d")
_UNITS = ("ratio", "ratio")


def _sample(
    trade_date: date,
    index: int,
    *,
    evidence_complete: bool = True,
    hard_filter_passed: bool = True,
) -> DailyCloseSourceSample:
    return DailyCloseSourceSample(
        trade_date=trade_date,
        label_maturity_date=trade_date + timedelta(days=1),
        code=f"60{index:04d}",
        board="main",
        feature_values=(index / 100.0, index / 200.0),
        net_excess_returns=(0.01, 0.007, 0.002),
        hard_filter_passed=hard_filter_passed,
        hard_filter_evidence_complete=evidence_complete,
        filter_evidence_hash=_HASH_A if evidence_complete else None,
        source_row_hash=_HASH_B,
    )


def test_dataset_rejects_incomplete_or_failed_hard_filter_samples_and_records_counts() -> None:
    first = date(2024, 1, 2)
    source = (
        _sample(first, 1),
        _sample(first, 2, evidence_complete=False),
        _sample(first, 3, hard_filter_passed=False),
    )

    dataset = build_feature_dataset(
        source,
        feature_names=_FEATURES,
        feature_units=_UNITS,
        source_archive_hash=_HASH_A,
        filter_spec_hash=_HASH_B,
    )

    assert tuple(row.code for row in dataset.rows) == ("600001",)
    assert dataset.manifest.accepted_rows == 1
    assert dataset.manifest.rejected_filter_evidence_rows == 1
    assert dataset.manifest.rejected_hard_filter_rows == 1
    assert dataset.manifest.production_authority is False
    assert dataset.production_authority is False


def test_manifest_keeps_a_complete_trading_day_when_its_whole_population_is_rejected() -> None:
    first = date(2024, 1, 2)
    second = date(2024, 1, 3)

    dataset = build_feature_dataset(
        (_sample(first, 1), _sample(second, 2, evidence_complete=False)),
        feature_names=_FEATURES,
        feature_units=_UNITS,
        source_archive_hash=_HASH_A,
        filter_spec_hash=_HASH_B,
    )

    assert dataset.manifest.trading_dates == (first, second)
    assert tuple(row.trade_date for row in dataset.rows) == (first,)


def test_fold_training_rows_exclude_labels_not_mature_before_validation_start() -> None:
    days = tuple(date(2024, 1, 1) + timedelta(days=index) for index in range(260))
    dataset = build_feature_dataset(
        tuple(_sample(day, index) for index, day in enumerate(days)),
        feature_names=_FEATURES,
        feature_units=_UNITS,
        source_archive_hash=_HASH_A,
        filter_spec_hash=_HASH_B,
    )
    split = split_complete_trading_dates(days)
    first_fold = build_expanding_walk_forward(split.development_dates)[0]

    training_rows = select_mature_fold_training_rows(dataset, first_fold)

    assert training_rows
    assert all(row.trade_date in first_fold.training_dates for row in training_rows)
    assert all(row.label_maturity_date < first_fold.validation_dates[0] for row in training_rows)
    assert max(row.trade_date for row in training_rows) < max(first_fold.training_dates)

    confirmation_rows = select_mature_training_rows(
        dataset,
        split.development_dates,
        prediction_start=split.confirmation_dates[0],
    )
    assert all(row.label_maturity_date < split.confirmation_dates[0] for row in confirmation_rows)


def _artifact() -> CandidateModelArtifact:
    return CandidateModelArtifact(
        model_id="tomorrow_daily_close_candidate_v1",
        candidate_id="ridge_lightgbm_ensemble_with_shrinkage",
        base_model_kind="ridge_lightgbm_ensemble",
        manifest_hash=_HASH_A,
        filter_spec_hash=_HASH_A,
        confirmation_report_hash=_HASH_B,
        feature_names=_FEATURES,
        feature_units=_UNITS,
        preprocessing_means=(0.1, 0.2),
        preprocessing_scales=(1.0, 2.0),
        ridge_intercept=0.01,
        ridge_coefficients=(0.3, -0.2),
        lightgbm_model_text="tree\nend",
        lightgbm_best_iteration=7,
        stratum_corrections=(StratumCorrection("board", "main", 100, 50, 500.0, 0.001),),
        stock_residual_corrections=(),
        trained_from=date(2020, 1, 2),
        trained_through=date(2024, 12, 31),
        dependencies=(ModelDependencyVersion("python", "3.10"),),
    )


def test_candidate_artifact_and_terminal_report_are_tamper_evident_and_non_production() -> None:
    artifact = _artifact()
    metrics = ValidationMetrics(
        evaluated_trade_dates=200,
        evaluated_rows=10_000,
        net_excess_return_20bp=0.002,
        net_excess_return_50bp=0.001,
        bootstrap_lower_bound_20bp=0.0001,
        bootstrap_lower_bound_50bp=0.00005,
        control_severe_loss_rate=0.04,
        candidate_severe_loss_rate=0.03,
        turnover_increase=0.01,
        rank_ic=0.05,
        top_bottom_quintile_spread=0.004,
    )
    report = ValidationReport(
        status="historical_daily_close_proxy_validated",
        manifest_hash=_HASH_A,
        candidate_model_artifact_hash=artifact.content_hash,
        metrics=metrics,
        failure_reasons=(),
    )

    assert len(artifact.content_hash) == 64
    assert len(report.content_hash) == 64
    assert artifact.production_authority is False
    assert report.production_authority is False
    with pytest.raises(ValueError, match="cannot authorize production"):
        replace(artifact, production_authority=True)
    with pytest.raises(ValueError, match="requires no failure reasons"):
        replace(report, failure_reasons=("unexpected_failure",))
    with pytest.raises(ValueError, match="return and risk gates"):
        replace(report, metrics=replace(metrics, bootstrap_lower_bound_50bp=-0.00001))
    with pytest.raises(ValueError, match="return and risk gates"):
        replace(report, metrics=replace(metrics, candidate_severe_loss_rate=0.05))


def test_candidate_artifact_represents_each_registered_base_model_without_fake_placeholders() -> None:
    ensemble = _artifact()
    ridge = replace(
        ensemble,
        candidate_id="ridge",
        base_model_kind="ridge",
        lightgbm_model_text=None,
        lightgbm_best_iteration=None,
    )
    lightgbm = replace(
        ensemble,
        candidate_id="lightgbm",
        base_model_kind="lightgbm",
        ridge_intercept=None,
        ridge_coefficients=None,
    )

    assert ridge.base_model_kind == "ridge"
    assert lightgbm.base_model_kind == "lightgbm"
    with pytest.raises(ValueError, match="base model payload"):
        replace(ridge, lightgbm_model_text="fake", lightgbm_best_iteration=1)
    with pytest.raises(ValueError, match="base model payload"):
        replace(lightgbm, ridge_intercept=0.0, ridge_coefficients=(0.0, 0.0))


def test_shrinkage_corrections_enforce_preregistered_sample_and_cap_contracts() -> None:
    correction = StockResidualCorrection(
        code="600001",
        sample_count=250,
        distinct_trade_dates=120,
        shrinkage_constant=1_000.0,
        prediction_cross_section_stddev=0.02,
        correction=0.002,
    )

    assert correction.correction == 0.002
    with pytest.raises(ValueError, match="shrinkage contract"):
        replace(correction, shrinkage_constant=999.0)
    with pytest.raises(ValueError, match="10%"):
        replace(correction, correction=0.00201)
    with pytest.raises(ValueError, match="stratum correction"):
        StratumCorrection("board", "main", 49, 50, 500.0, 0.001)


def test_rejected_and_insufficient_reports_require_bounded_reasons() -> None:
    metrics = ValidationMetrics.empty()

    rejected = ValidationReport(
        status="historical_rejected",
        manifest_hash=_HASH_A,
        candidate_model_artifact_hash=_HASH_B,
        metrics=metrics,
        failure_reasons=("terminal_net_return_gate_failed",),
    )
    insufficient = ValidationReport(
        status="historical_data_insufficient",
        manifest_hash=_HASH_A,
        candidate_model_artifact_hash=None,
        metrics=metrics,
        failure_reasons=("terminal_trade_dates_below_200",),
    )

    assert rejected.status == "historical_rejected"
    assert insufficient.status == "historical_data_insufficient"
    with pytest.raises(ValueError, match="requires bounded failure reasons"):
        replace(rejected, failure_reasons=())
