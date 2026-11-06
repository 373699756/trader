from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.training.training_resources import TrainingSampleSettingsFixture
from trader.training.application.tomorrow_training import ProductionTrainingWindow, TomorrowTrainingWindow
from trader.training.domain.final_fit import BuildFinalFitDatePlan, FinalFitDatePlan
from trader.training.domain.training_dates import BuildTrainingDateSplit
from trader.training.infra.profile.v3.contracts import D25_HEAD_CONTRACT, TOMORROW_HEAD_CONTRACT
from trader.training.infra.profile.v3.training_sample_index import SQLiteV3TrainingSampleIndex
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.sample_index import FinalFitIndustryCounts, SQLiteTrainingSampleIndex, TrainingSample
from trader.training.infra.sample_sequences import SampleSequenceLayout


def _calendar() -> tuple[date, ...]:
    return tuple(date(2020, 1, 1) + timedelta(days=index) for index in range(1_600))


def _sample(
    day: date, code: str = "600001", industry: str = "银行", *, target: float | None = 0.02,
) -> TrainingSample:
    return TrainingSample(code, day, "main", industry, 1e7, (0.1,) * 6, (target, 0.03, 0.04, 0.05, 0.06, 0.045))


def test_final_fit_uses_trading_maturity_and_an_explicit_source_cutoff() -> None:
    calendar = tuple(
        date(2026, 10, 19) + timedelta(days=index)
        for index in range(19)
        if (date(2026, 10, 19) + timedelta(days=index)).weekday() < 5
    )
    cutoff = calendar[-2]
    tomorrow = BuildFinalFitDatePlan(calendar, source_cutoff=cutoff, fit_sessions=3, maturity_sessions=1)
    d25 = BuildFinalFitDatePlan(calendar, source_cutoff=cutoff, fit_sessions=3, maturity_sessions=5)
    assert tomorrow.fit_dates == calendar[-5:-2]
    assert d25.fit_dates == calendar[-9:-6]
    assert tomorrow.content_hash == BuildFinalFitDatePlan(
        calendar, source_cutoff=cutoff, fit_sessions=3, maturity_sessions=1,
    ).content_hash
    assert tomorrow.content_hash != d25.content_hash


@pytest.mark.parametrize(
    "calendar,cutoff,fit_sessions,maturity",
    (
        ((date(2026, 1, 1), date(2026, 1, 1)), date(2026, 1, 1), 1, 1),
        ((date(2026, 1, 2), date(2026, 1, 1)), date(2026, 1, 2), 1, 1),
        ((datetime(2026, 1, 1),), date(2026, 1, 1), 1, 1),
        ((date(2026, 1, 1),), date(2026, 1, 2), 1, 1),
        ((date(2026, 1, 1),), date(2026, 1, 1), True, 1),
        ((date(2026, 1, 1),), date(2026, 1, 1), 1, True),
        ((date(2026, 1, 1),), date(2026, 1, 1), 1, 2),
    ),
)
def test_final_fit_rejects_invalid_calendar_or_settings(calendar, cutoff, fit_sessions, maturity) -> None:
    with pytest.raises(ValueError, match="final_fit_calendar_or_settings_invalid"):
        BuildFinalFitDatePlan(calendar, source_cutoff=cutoff, fit_sessions=fit_sessions, maturity_sessions=maturity)


def test_final_fit_rejects_insufficient_history_and_an_unmatured_direct_plan() -> None:
    calendar = _calendar()
    with pytest.raises(ValueError, match="final_fit_calendar_insufficient"):
        BuildFinalFitDatePlan(calendar, source_cutoff=calendar[2], fit_sessions=1, maturity_sessions=5)
    with pytest.raises(ValueError, match="final_fit_date_plan_invalid"):
        FinalFitDatePlan(calendar[-1], 5, (calendar[-3],))


def test_production_window_reads_through_source_end_without_opening_research_window() -> None:
    calendar = _calendar()
    split = BuildTrainingDateSplit(calendar, parent_manifest_hash="a" * 64)
    research = TomorrowTrainingWindow(split)
    production = ProductionTrainingWindow(split, calendar)
    production.require_readable(split.point_in_time_holdout_dates)
    assert production.readable_dates == frozenset(calendar[calendar.index(split.warmup_dates[0]):])
    with pytest.raises(ValueError, match="point-in-time holdout is not readable"):
        research.require_readable((calendar[-1],))
    with pytest.raises(ValueError, match="production_training_date_outside_source"):
        production.require_readable((calendar[-1] + timedelta(days=1),))
    with pytest.raises(ValueError, match="production_training_source_calendar_mismatch"):
        ProductionTrainingWindow(split, calendar[:-1])


def test_final_fit_counts_require_head_date_coverage_and_keep_industry_gaps(tmp_path: Path) -> None:
    calendar = _calendar()
    split = BuildTrainingDateSplit(calendar, parent_manifest_hash="a" * 64)
    plan = BuildFinalFitDatePlan(calendar, source_cutoff=calendar[-1], fit_sessions=2, maturity_sessions=1)
    with SQLiteV3TrainingSampleIndex(
        tmp_path / "samples.sqlite3", sqlite_policy=SqlitePolicyFixture(), sample_settings=TrainingSampleSettingsFixture(),
    ) as samples:
        samples.add_final((
            _sample(plan.fit_dates[0], industry="银行"),
            _sample(plan.fit_dates[1], industry="软件"),
            _sample(plan.fit_dates[1], "600002", industry="银行", target=None),
            _sample(calendar[-1], "600003", industry="银行"),
        ))
        with pytest.raises(RuntimeError, match="final_fit_requires_sealed_samples"):
            samples.final_fit_counts(TOMORROW_HEAD_CONTRACT, plan)
        samples.prepare_for_model_fitting(split)
        assert samples.final_fit_counts(TOMORROW_HEAD_CONTRACT, plan) == (
            FinalFitIndustryCounts("软件", 1, 1), FinalFitIndustryCounts("银行", 1, 1),
        )
        with pytest.raises(ValueError, match="final_fit_head_maturity_mismatch"):
            samples.final_fit_counts(D25_HEAD_CONTRACT, plan)
        missing = BuildFinalFitDatePlan(calendar, source_cutoff=calendar[-1], fit_sessions=3, maturity_sessions=1)
        with pytest.raises(ValueError, match="final_fit_effective_dates_insufficient"):
            samples.final_fit_counts(TOMORROW_HEAD_CONTRACT, missing)
        assert samples.industry_counts(TOMORROW_HEAD_CONTRACT) == ()
        with pytest.raises(ValueError, match="training_holdout_is_sealed"):
            tuple(samples.iter_batches("银行", TOMORROW_HEAD_CONTRACT, "holdout"))


def test_final_fit_batches_filter_exact_dates_null_labels_and_preserve_order(tmp_path: Path) -> None:
    calendar = _calendar()
    split = BuildTrainingDateSplit(calendar, parent_manifest_hash="a" * 64)
    plan = FinalFitDatePlan(calendar[-1], 1, (calendar[-6], calendar[-3]))
    with SQLiteV3TrainingSampleIndex(
        tmp_path / "samples.sqlite3", sqlite_policy=SqlitePolicyFixture(), sample_settings=TrainingSampleSettingsFixture(),
    ) as samples:
        samples.add_final((
            _sample(calendar[-3], "600002", target=0.2),
            _sample(calendar[-6], "600003", target=None),
            _sample(calendar[-3], "600001", target=0.1),
            _sample(calendar[-6], "600001", target=0.3),
            _sample(calendar[-5], "600004", target=99.0),
        ))
        samples.prepare_for_model_fitting(split)
        batches = tuple(samples.iter_final_fit_batches("银行", TOMORROW_HEAD_CONTRACT, plan, batch_size=2))
        assert [len(batch.labels) for batch in batches] == [2, 1]
        assert tuple(day for batch in batches for day in batch.trade_dates) == (calendar[-6], calendar[-3], calendar[-3])
        assert tuple(code for batch in batches for code in batch.codes) == ("600001", "600001", "600002")
        assert [value for batch in batches for value in batch.labels.tolist()] == [0.3, 0.1, 0.2]
        with pytest.raises(ValueError, match="final_fit_batch_arguments_invalid"):
            tuple(samples.iter_final_fit_batches("银行", TOMORROW_HEAD_CONTRACT, plan, batch_size=True))


@pytest.mark.parametrize("failure", (None, RuntimeError, InterruptedError))
def test_final_fit_matrix_releases_mappings_on_success_failure_and_cancel(tmp_path: Path, failure) -> None:
    calendar = _calendar()
    split = BuildTrainingDateSplit(calendar, parent_manifest_hash="a" * 64)
    plan = BuildFinalFitDatePlan(calendar, source_cutoff=calendar[-1], fit_sessions=1, maturity_sessions=1)
    with SQLiteV3TrainingSampleIndex(
        tmp_path / "samples.sqlite3", sqlite_policy=SqlitePolicyFixture(),
        sample_settings=TrainingSampleSettingsFixture(matrix_memory_bytes=1),
    ) as samples:
        samples.add_final((_sample(plan.fit_dates[0]),))
        samples.prepare_for_model_fitting(split)
        counts = samples.final_fit_counts(TOMORROW_HEAD_CONTRACT, plan)[0]
        buffers = []
        try:
            with samples.final_fit_data(counts, TOMORROW_HEAD_CONTRACT, plan) as matrix:
                buffers.extend((matrix.features.base, matrix.labels.base))
                assert matrix.features.tolist() == [[0.1] * 6]
                assert matrix.labels.tolist() == [0.02]
                assert tuple(tmp_path.glob("sample-fit-*"))
                if failure is not None:
                    raise failure("fixture exit")
        except (RuntimeError, InterruptedError) as exc:
            assert failure is not None and type(exc) is failure
        assert all(buffer.closed for buffer in buffers)
        assert not tuple(tmp_path.glob("sample-fit-*"))
        with pytest.raises(ValueError, match="final_fit_industry_counts_changed"):
            with samples.final_fit_data(replace(counts, rows=2), TOMORROW_HEAD_CONTRACT, plan):
                pytest.fail("a changed count must be rejected before matrix allocation")


def test_final_fit_tcn_batches_reconstruct_the_existing_sequence_storage(tmp_path: Path) -> None:
    calendar = _calendar()
    split = BuildTrainingDateSplit(calendar, parent_manifest_hash="a" * 64)
    contract = V5_TRAINING_PROFILE.heads[0]
    plan = BuildFinalFitDatePlan(calendar, source_cutoff=calendar[-1], fit_sessions=2, maturity_sessions=1)
    features = (0.1,) * 6 + tuple(index / 100 for index in range(360))
    with SQLiteTrainingSampleIndex(
        tmp_path / "samples.sqlite3", contract.feature_manifest.names, sequence_layout=SampleSequenceLayout(6, 60, 6),
        sqlite_policy=SqlitePolicyFixture(), sample_settings=TrainingSampleSettingsFixture(),
    ) as samples:
        samples.add_final(tuple(replace(_sample(day), features=features) for day in plan.fit_dates))
        samples.prepare_for_model_fitting(split)
        counts = samples.final_fit_counts(contract, plan)[0]
        with samples.final_fit_data(counts, contract, plan) as matrix:
            np.testing.assert_array_equal(matrix.features, np.asarray([features, features]))
            np.testing.assert_array_equal(matrix.labels, [0.02, 0.02])
