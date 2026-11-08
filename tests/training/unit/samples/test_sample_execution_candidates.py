from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.training.training_resources import TrainingSampleSettingsFixture
from tests.helpers.training.current_industry import CurrentIndustrySnapshotFixture
from trader.infra.sqlite_connection import SqliteResult
from trader.training.domain.training_dates import BuildTrainingDateSplit, TrainingDateSplit
from trader.training.infra import model_fitting
from trader.training.infra.execution_options import TrainingExecutionOptions
from trader.training.infra.model_bundles.bundle_codec import TrainedRidgeModelSnapshot
from trader.training.infra.model_evaluation import CalibrationResult
from trader.training.infra.model_fitting import FitIndustryModels, RidgeFittingParameters
from trader.training.infra.model_fitting_results import FittedIndustryModel
from trader.training.infra.profile.v3.contracts import HEAD_CONTRACTS
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.sample_index import SQLiteTrainingSampleIndex, TrainingSample, TrainingSampleBatch
from trader.training.infra.sample_sequences import SampleSequenceLayout


@pytest.fixture(scope="module")
def split() -> TrainingDateSplit:
    return BuildTrainingDateSplit(
        tuple(date(2021, 1, 1) + timedelta(days=offset) for offset in range(1600)), parent_manifest_hash="a" * 64
    )


def _sample(number: int, day: date, *, width: int = 6, industry: str = "银行") -> TrainingSample:
    return TrainingSample(
        f"{600000 + number:06d}",
        day,
        "main",
        industry,
        1_000_000.0,
        tuple(-0.0 if position % 11 == 0 else (number % (position + 7)) / 31 for position in range(width)),
        (None if number % 3 == 0 else number / 1000, 0.02, 0.03, 0.04, 0.05,
         None if number % 7 == 0 else (number % 13) / 200),
    )


@pytest.mark.parametrize(
    "enabled,budget,threshold,uses_ram",
    ((False, 312, 312, False), (True, 0, 312, False), (True, 311, 312, False),
     (True, 312, 311, False), (True, 312, 312, True)),
)
def test_shared_matrix_requires_explicit_option_threshold_and_granted_budget(
    tmp_path: Path, split: TrainingDateSplit, enabled: bool, budget: int, threshold: int, uses_ram: bool
) -> None:
    with SQLiteTrainingSampleIndex(
        tmp_path / "samples.sqlite3",
        HEAD_CONTRACTS[0].feature_manifest.names,
        sample_settings=TrainingSampleSettingsFixture(matrix_memory_bytes=threshold),
        sqlite_policy=SqlitePolicyFixture(),
        execution_options=TrainingExecutionOptions(shared_matrix_memory=enabled),
        shared_matrix_budget_bytes=budget,
    ) as samples:
        samples.add_final((
            _sample(1, split.model_fit_dates[0]),
            _sample(2, split.early_stopping_dates[0]),
            replace(_sample(3, split.calibration_dates[0]), targets=(None,) * 6),
            _sample(4, split.confirmation_dates[0]),
            _sample(5, split.daily_proxy_holdout_dates[0]),
        ))
        samples.prepare_for_model_fitting(split)
        assert samples.shared_matrix_memory_bytes() == (312 if enabled and threshold >= 312 else 0)
        with samples.share_industry("银行"):
            assert bool(tuple(tmp_path.glob("sample-shared-*"))) is not uses_ram
            assert samples._shared_rows is not None
            assert not samples._shared_rows.features.flags.writeable
            first = samples.industry_counts(HEAD_CONTRACTS[0])[0]
            second = samples.industry_counts(HEAD_CONTRACTS[1])[0]
            with samples.industry_data(first, HEAD_CONTRACTS[0]) as data:
                data.training.features[:] = 99
            with samples.industry_data(second, HEAD_CONTRACTS[1]) as data:
                np.testing.assert_array_equal(data.training.features, [_sample(1, split.model_fit_dates[0]).features[1:]])
        assert samples._shared_rows is None
        assert not tuple(tmp_path.glob("sample-shared-*"))


def test_shared_budget_can_only_be_granted_once_before_the_first_read(tmp_path: Path, split: TrainingDateSplit) -> None:
    with SQLiteTrainingSampleIndex(
        tmp_path / "samples.sqlite3",
        HEAD_CONTRACTS[0].feature_manifest.names,
        sample_settings=TrainingSampleSettingsFixture(),
        sqlite_policy=SqlitePolicyFixture(),
        execution_options=TrainingExecutionOptions(shared_matrix_memory=True),
    ) as samples:
        samples.add_final((_sample(1, split.model_fit_dates[0]),))
        samples.prepare_for_model_fitting(split)
        for invalid in (-1, True, 0.5):
            with pytest.raises(ValueError, match="training_shared_matrix_budget_invalid"):
                samples.configure_shared_matrix_budget(invalid)
        samples.configure_shared_matrix_budget(0)
        with pytest.raises(RuntimeError, match="already_configured"):
            samples.configure_shared_matrix_budget(104)
        with pytest.raises(InterruptedError), samples.share_industry("银行"):
            assert tuple(tmp_path.glob("sample-shared-*"))
            raise InterruptedError("fixture cancellation")
        assert samples._shared_rows is None
        assert not tuple(tmp_path.glob("sample-shared-*"))


def test_shared_budget_is_frozen_after_a_default_read(tmp_path: Path, split: TrainingDateSplit) -> None:
    with SQLiteTrainingSampleIndex(
        tmp_path / "samples.sqlite3",
        HEAD_CONTRACTS[0].feature_manifest.names,
        sample_settings=TrainingSampleSettingsFixture(),
        sqlite_policy=SqlitePolicyFixture(),
        execution_options=TrainingExecutionOptions(shared_matrix_memory=True),
    ) as samples:
        samples.prepare_for_model_fitting(split)
        with samples.share_industry("empty"):
            assert tuple(tmp_path.glob("sample-shared-*"))
        with pytest.raises(RuntimeError, match="already_configured"):
            samples.configure_shared_matrix_budget(104)


def _assert_same_batches(actual: list[TrainingSampleBatch], expected: tuple[TrainingSampleBatch, ...]) -> None:
    assert [len(batch.labels) for batch in actual] == [len(batch.labels) for batch in expected]
    for observed, reference in zip(actual, expected, strict=True):
        np.testing.assert_array_equal(observed.features.view(np.uint64), reference.features.view(np.uint64))
        np.testing.assert_array_equal(observed.labels.view(np.uint64), reference.labels.view(np.uint64))
        assert observed.trade_dates == reference.trade_dates
        assert observed.codes == reference.codes
        assert observed.industries == reference.industries


@pytest.mark.parametrize("sequence", (False, True))
def test_paired_validation_preserves_each_heads_population_batches_and_float_bits(
    tmp_path: Path, split: TrainingDateSplit, sequence: bool
) -> None:
    contracts = V5_TRAINING_PROFILE.heads if sequence else HEAD_CONTRACTS
    width = len(contracts[0].feature_manifest.names)
    with SQLiteTrainingSampleIndex(
        tmp_path / "samples.sqlite3",
        contracts[0].feature_manifest.names,
        sample_settings=TrainingSampleSettingsFixture(),
        sqlite_policy=SqlitePolicyFixture(),
        sequence_layout=SampleSequenceLayout(6, 60, 6) if sequence else None,
    ) as samples:
        samples.add_final(reversed(tuple(
            _sample(number, split.confirmation_dates[number % 3], width=width,
                    industry="银行" if number % 2 else "软件")
            for number in range(777)
        )))
        samples.add_final((_sample(999, split.daily_proxy_holdout_dates[0], width=width),))
        samples.prepare_for_model_fitting(split)
        expected = tuple(tuple(samples.iter_validation_batches(contract)) for contract in contracts)
        statements: list[str] = []
        samples._connection.observe_statements(statements.append)
        actual: tuple[list[TrainingSampleBatch], ...] = ([], [])
        for head, batch in samples.iter_validation_pairs(contracts):
            actual[head].append(batch)
        samples._connection.observe_statements(None)
        assert sum("SELECT s.trade_date,s.code,s.industry" in statement for statement in statements) == 1
        for observed, reference in zip(actual, expected, strict=True):
            _assert_same_batches(observed, reference)
            assert len(observed[0].labels) == 256
        if sequence:
            assert np.signbit(actual[0][0].features[:, 11]).all()
        single = [batch for head, batch in samples.iter_validation_pairs((contracts[1],)) if head == 0]
        _assert_same_batches(single, expected[1])


def test_paired_validation_closes_its_cursor_on_consumer_cancellation(
    tmp_path: Path, split: TrainingDateSplit, monkeypatch: pytest.MonkeyPatch
) -> None:
    with SQLiteTrainingSampleIndex(
        tmp_path / "samples.sqlite3",
        HEAD_CONTRACTS[0].feature_manifest.names,
        sample_settings=TrainingSampleSettingsFixture(),
        sqlite_policy=SqlitePolicyFixture(),
    ) as samples:
        samples.add_final(_sample(number, split.confirmation_dates[0]) for number in range(400))
        samples.prepare_for_model_fitting(split)
        stream = samples.iter_validation_pairs(HEAD_CONTRACTS)
        assert next(stream)[1].features.shape[0] == 256
        closed: list[bool] = []
        close = SqliteResult.close

        def observed_close(current: SqliteResult) -> None:
            closed.append(True)
            close(current)

        monkeypatch.setattr(SqliteResult, "close", observed_close)
        stream.close()
        assert closed == [True]
        assert samples.count() == 400


def test_shared_candidates_keep_complete_ridge_results_equal_to_the_baseline(
    tmp_path: Path, split: TrainingDateSplit
) -> None:
    parameters = RidgeFittingParameters(minimum_industry_training_rows=6, penalty=0.1)
    rows = tuple(
        _sample(number, day, industry=industry)
        for industry in ("银行", "软件")
        for day in (*split.model_fit_dates[:3], *split.calibration_dates[:3], *split.confirmation_dates[:3])
        for number in range(1, 130)
    ) + (_sample(999, split.confirmation_dates[0], industry="仅验证"),)
    results = []
    for enabled in (False, True):
        with SQLiteTrainingSampleIndex(
            tmp_path / f"samples-{enabled}.sqlite3",
            HEAD_CONTRACTS[0].feature_manifest.names,
            sample_settings=TrainingSampleSettingsFixture(),
            sqlite_policy=SqlitePolicyFixture(),
            execution_options=TrainingExecutionOptions(shared_matrix_memory=enabled, shared_validation=enabled),
        ) as samples:
            # Codes remain unique within each date across both industry populations.
            samples.add_final(replace(row, code=f"{position:06d}") for position, row in enumerate(rows))
            samples.prepare_for_model_fitting(split)
            if enabled:
                samples.configure_shared_matrix_budget(samples.shared_matrix_memory_bytes())
            results.append(FitIndustryModels(samples, split, HEAD_CONTRACTS, parameters, compute_threads=1,
                                             industry_snapshot=CurrentIndustrySnapshotFixture(())))
    assert all(head.models for head in results[0])
    assert results[1] == results[0]


def test_shared_validation_closes_the_stream_when_prediction_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []

    class Samples:
        def iter_validation_pairs(self, contracts):
            assert contracts == (HEAD_CONTRACTS[0],)
            try:
                yield 0, TrainingSampleBatch(np.ones((1, 6)), np.ones(1), (date(2026, 1, 5),), ("600001",), ("银行",))
            finally:
                closed.append(True)

    def failed_prediction(evaluation, batch) -> None:
        raise InterruptedError("fixture cancellation")

    monkeypatch.setattr(model_fitting._ValidationEvaluation, "add_batch", failed_prediction)
    fitted = FittedIndustryModel(
        "银行",
        TrainedRidgeModelSnapshot((0.0,) * 6, (1.0,) * 6, 1.0, 1.0, 0.0, (1.0,) * 6),
        10,
        CalibrationResult(2, 2, 1.0, 1.0, 1.0),
    )
    state = model_fitting._HeadFitState(HEAD_CONTRACTS[0], (), [fitted], [])
    with pytest.raises(InterruptedError):
        model_fitting._evaluate_validation_pairs(Samples(), (state,), None, 1)
    assert closed == [True]
