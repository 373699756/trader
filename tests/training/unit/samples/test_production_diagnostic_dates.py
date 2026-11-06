from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.training.training_resources import TrainingSampleSettingsFixture
from trader.training.application.tomorrow_training import ProductionTrainingWindow, TomorrowTrainingWindow
from trader.training.domain.final_fit import BuildFinalFitDatePlan
from trader.training.domain.production_diagnostic_dates import (
    BuildProductionDiagnosticDatePlan,
    TrainingSampleDatePartitions,
)
from trader.training.domain.training_dates import BuildTrainingDateSplit
from trader.training.infra.profile.v3.contracts import D25_HEAD_CONTRACT, TOMORROW_HEAD_CONTRACT
from trader.training.infra.profile.v3.training_sample_index import SQLiteV3TrainingSampleIndex
from trader.training.infra.sample_index import TrainingSample


def _calendar(count: int = 1_600) -> tuple[date, ...]:
    start = date(2020, 1, 1)
    return tuple(start + timedelta(days=offset) for offset in range(count * 2) if (start + timedelta(days=offset)).weekday() < 5)[:count]


def _plan(calendar: tuple[date, ...], *, early: int = 20, excludes_signal: bool = False, history: int = 61):
    return BuildProductionDiagnosticDatePlan(
        calendar, source_cutoff=calendar[-1], fit_sessions=61, early_stopping_sessions=early,
        history_sessions=history, history_excludes_signal=excludes_signal,
    )


@pytest.mark.parametrize("early", (0, 20))
@pytest.mark.parametrize("excludes_signal", (False, True))
def test_production_diagnostic_uses_latest_common_t5_and_only_recent_isolated_dates(early, excludes_signal) -> None:
    calendar = _calendar()
    plan = _plan(calendar, early=early, excludes_signal=excludes_signal)
    assert plan.source_cutoff == calendar[-1] and plan.maturity_sessions == 5
    assert plan.validation_dates == calendar[-25:-5]
    assert plan.embargo_dates[-1] == calendar[-30:-25]
    assert plan.calibration_dates == calendar[-50:-30]
    if early:
        assert plan.embargo_dates[1] == calendar[-55:-50]
        assert plan.early_stopping_dates == calendar[-75:-55]
        assert plan.embargo_dates[0] == calendar[-80:-75]
        assert plan.fitting_dates == calendar[-141:-80]
    else:
        assert plan.early_stopping_dates == ()
        assert plan.embargo_dates[0] == calendar[-55:-50]
        assert plan.fitting_dates == calendar[-116:-55]
    end = calendar.index(plan.fitting_dates[0])
    assert plan.warmup_dates == calendar[end - (61 if excludes_signal else 60):end]
    partitions = TrainingSampleDatePartitions(plan)
    assert partitions.training == plan.fitting_dates and partitions.validation == plan.validation_dates
    assert partitions.holdout == ()
    assert plan.content_hash == _plan(calendar, early=early, excludes_signal=excludes_signal).content_hash
    with pytest.raises(FrozenInstanceError):
        partitions.holdout = calendar[-200:]
    for maturity in (1, 5):
        final = BuildFinalFitDatePlan(calendar, source_cutoff=calendar[-1], fit_sessions=61, maturity_sessions=maturity)
        assert final.fit_dates[-1] == calendar[-1 - maturity]
        assert final.fit_dates[-1] >= plan.validation_dates[-1] > plan.fitting_dates[-1]


def test_production_window_checks_trading_calendar_maturity_and_excludes_rows_after_cutoff() -> None:
    calendar = _calendar()
    source_cutoff = calendar[-4]
    plan = BuildProductionDiagnosticDatePlan(
        calendar, source_cutoff=source_cutoff, fit_sessions=61, early_stopping_sessions=20, history_sessions=61,
    )
    assert plan.validation_dates[-1] == calendar[-9]
    window = ProductionTrainingWindow(plan, calendar)
    assert window.readable_dates == frozenset(calendar[calendar.index(plan.warmup_dates[0]):-3])
    with pytest.raises(ValueError, match="production_training_date_outside_source"):
        window.require_readable((calendar[-3],))
    with pytest.raises(ValueError, match="production_training_source_calendar_mismatch"):
        ProductionTrainingWindow(plan, tuple(day for day in calendar if day != plan.embargo_dates[0][0]))
    shifted_cutoff = replace(plan, source_cutoff=calendar[-3])
    with pytest.raises(ValueError, match="production_training_source_calendar_mismatch"):
        ProductionTrainingWindow(shifted_cutoff, calendar)


def test_production_diagnostics_do_not_require_the_research_minimum_calendar() -> None:
    calendar = _calendar(201)
    plan = _plan(calendar)
    assert len(plan.fitting_dates) == 61 and plan.warmup_dates[0] == calendar[0]
    with pytest.raises(ValueError, match="production_diagnostic_calendar_insufficient"):
        _plan(calendar[:-1])
    with pytest.raises(ValueError, match="production_diagnostic_calendar_insufficient"):
        _plan(calendar, excludes_signal=True)
    without_warmup = _plan(calendar, history=1)
    assert without_warmup.warmup_dates == ()
    assert min(ProductionTrainingWindow(without_warmup, calendar).readable_dates) == without_warmup.fitting_dates[0]


@pytest.mark.parametrize(
    "settings",
    (
        {"fit_sessions": True}, {"fit_sessions": 0}, {"early_stopping_sessions": True},
        {"early_stopping_sessions": 19}, {"history_sessions": False}, {"history_sessions": 0},
        {"history_excludes_signal": 1}, {"source_cutoff": datetime(2026, 1, 1)},
        {"source_cutoff": date(1900, 1, 1)},
    ),
)
def test_production_diagnostic_rejects_invalid_settings(settings) -> None:
    calendar = _calendar()
    values = dict(source_cutoff=calendar[-1], fit_sessions=61, early_stopping_sessions=20, history_sessions=61)
    values.update(settings)
    with pytest.raises(ValueError, match="production_diagnostic_calendar_or_settings_invalid"):
        BuildProductionDiagnosticDatePlan(calendar, **values)


def test_production_diagnostic_rejects_non_date_duplicate_unordered_or_oversized_calendars() -> None:
    calendar = _calendar()
    for invalid in (
        (), calendar + (calendar[-1],), tuple(reversed(calendar)), (datetime(2026, 1, 1),), _calendar(2_001),
    ):
        with pytest.raises(ValueError, match="production_diagnostic_calendar_or_settings_invalid"):
            BuildProductionDiagnosticDatePlan(
                invalid, source_cutoff=calendar[-1], fit_sessions=61, early_stopping_sessions=20, history_sessions=61,
            )


def test_production_diagnostic_direct_values_reject_bad_partitions_and_maturity() -> None:
    plan = _plan(_calendar())
    for changes in (
        {"maturity_sessions": 1}, {"validation_dates": plan.validation_dates[:-1]},
        {"embargo_dates": plan.embargo_dates[:-1]}, {"calibration_dates": plan.validation_dates},
        {"fitting_dates": tuple(reversed(plan.fitting_dates))},
        {"source_cutoff": plan.validation_dates[-1]},
    ):
        with pytest.raises(ValueError, match="production_diagnostic_date_plan_invalid"):
            replace(plan, **changes)


def test_research_projection_preserves_all_old_partitions_and_sealed_window() -> None:
    calendar = _calendar()
    split = BuildTrainingDateSplit(calendar, parent_manifest_hash="a" * 64)
    partitions = TrainingSampleDatePartitions(split)
    assert partitions.training == split.model_fit_dates
    assert partitions.early_stopping == split.early_stopping_dates
    assert partitions.calibration == split.calibration_dates
    assert partitions.validation == split.confirmation_dates
    assert partitions.holdout == split.daily_proxy_holdout_dates
    with pytest.raises(ValueError, match="point-in-time holdout"):
        TomorrowTrainingWindow(split).require_readable((calendar[-1],))
    with pytest.raises(ValueError, match="production_training_source_dates_invalid"):
        ProductionTrainingWindow(split, calendar)


def test_sample_index_uses_production_partitions_without_changing_schema_or_final_fit_access(tmp_path: Path) -> None:
    calendar = _calendar()
    plan = _plan(calendar)
    final = BuildFinalFitDatePlan(calendar, source_cutoff=calendar[-1], fit_sessions=61, maturity_sessions=1)
    partitions = TrainingSampleDatePartitions(plan)
    days = tuple(sorted({*partitions.training, *partitions.early_stopping, *partitions.calibration,
                         *partitions.validation, *final.fit_dates}))
    with SQLiteV3TrainingSampleIndex(
        tmp_path / "samples.sqlite3", sqlite_policy=SqlitePolicyFixture(), sample_settings=TrainingSampleSettingsFixture(),
    ) as samples:
        samples.add_final(
            TrainingSample("600001", day, "main", "银行", 1e7, (0.1,) * 6, (0.1, 0.2, 0.3, 0.4, 0.5, 0.35))
            for day in days
        )
        samples.prepare_for_model_fitting(plan)
        samples.require_split(plan)
        for contract in (TOMORROW_HEAD_CONTRACT, D25_HEAD_CONTRACT):
            counts, = samples.industry_counts(contract)
            assert (counts.training, counts.early_stopping, counts.calibration, counts.validation, counts.holdout) == (
                61, 20, 20, 20, 0,
            )
            coverage = samples.coverage(contract)
            assert [(item.partition, item.expected_dates) for item in coverage] == [
                ("training", 61), ("early_stopping", 20), ("calibration", 20), ("validation", 20), ("holdout", 0),
            ]
            assert tuple(day for batch in samples.iter_validation_batches(contract) for day in batch.trade_dates) == plan.validation_dates
        counts, = samples.final_fit_counts(TOMORROW_HEAD_CONTRACT, final)
        assert counts.rows == counts.dates == 61
        research = BuildTrainingDateSplit(calendar, parent_manifest_hash="a" * 64)
        with pytest.raises(RuntimeError, match="split identity is inconsistent"):
            samples.require_split(research)
