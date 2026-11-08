"""Fit progress counts completed work independently of each head's workload ordering."""

from contextlib import contextmanager
from datetime import date, timedelta

import numpy as np
import pytest

from tests.helpers.training.current_industry import CurrentIndustrySnapshotFixture
from trader.training.application import learned_rank_training
from trader.training.application.tomorrow_training import TomorrowTrainingProgress, TrainingFitCounts, TrainingFitDetail
from trader.training.domain.final_fit import FinalFitDatePlan
from trader.training.domain.training_dates import BuildTrainingDateSplit
from trader.training.infra import model_fitting
from trader.training.infra.execution_options import TrainingExecutionOptions
from trader.training.infra.model_bundles.bundle_codec import TrainedRidgeModelSnapshot
from trader.training.infra.model_evaluation import CalibrationResult, PredictionMetrics
from trader.training.infra.model_fitting_results import FittedIndustryModel, IndustryFitResult
from trader.training.infra.profile.v3.contracts import HEAD_CONTRACTS
from trader.training.infra.sample_index import FinalFitIndustryCounts, TrainingIndustryCounts, TrainingSampleBatch

_FIRST = "capco:J66货币金融服务"
_SECOND = "capco:C39计算机通信和其他电子设备制造业"


@pytest.mark.parametrize("shared_validation", (False, True))
@pytest.mark.parametrize("second_head_full", (False, True))
def test_fit_progress_counts_actual_completed_tasks_with_different_head_orders(monkeypatch, shared_validation, second_head_full):
    split = BuildTrainingDateSplit(
        tuple(date(2021, 1, 1) + timedelta(days=offset) for offset in range(1500)),
        parent_manifest_hash="a" * 64,
    )
    counts = tuple(TrainingIndustryCounts(industry, 10, 2, 2, 3, 2) for industry in (_FIRST, _SECOND))
    industry = CurrentIndustrySnapshotFixture(
        ("600001", "600002"), sectors=(("J66", "货币金融服务"), ("C39", "计算机通信和其他电子设备制造业")),
    )
    events = []
    attempts = []
    batch = TrainingSampleBatch(
        np.zeros((6, 6)), np.zeros(6), (date(2025, 1, 1),) * 6,
        tuple(f"{number:06d}" for number in range(6)), (_FIRST,) * 3 + (_SECOND,) * 3,
    )

    class Samples:
        execution_options = TrainingExecutionOptions(shared_validation=shared_validation)

        def require_split(self, selected):
            assert selected is split

        def industry_counts(self, contract):
            if contract is HEAD_CONTRACTS[0]:
                return counts[::-1]
            return counts if second_head_full else counts[1:]

        @contextmanager
        def share_industry(self, industry, *, checkpoint):
            checkpoint()
            yield

        def iter_validation_batches(self, contract):
            if contract is HEAD_CONTRACTS[0] or second_head_full:
                yield batch
            else:
                yield TrainingSampleBatch(batch.features[3:], batch.labels[3:], batch.trade_dates[3:], batch.codes[3:], batch.industries[3:])

        def iter_validation_pairs(self, contracts):
            for head, contract in enumerate(contracts):
                for current in self.iter_validation_batches(contract):
                    yield head, current

    class Progress:
        def publish(self, event):
            events.append(event)

    def attempt(_samples, workload, contract, _parameters, heartbeat, report, _compute_threads):
        attempts.append((contract.strategy, workload.industry))
        for phase in ("batch_cache", "normalization", "training", "early_stopping", "calibration"):
            report(TrainingFitDetail(workload.industry, workload.training, phase, 1, 10, 1, 3))
            heartbeat()
        if (contract is HEAD_CONTRACTS[0]) == (workload.industry == _SECOND):
            return None, "calibration_prediction_variance_insufficient"
        return FittedIndustryModel(
            workload.industry, TrainedRidgeModelSnapshot((0.0,) * 6, (1.0,) * 6, 0.0, 1.0, 0.0, (0.0,) * 6),
            workload.training, CalibrationResult(2, 2, 1.0, 0.0, 1.0),
        ), ""

    monkeypatch.setattr(model_fitting, "_attempt_industry_fit", attempt)
    result = model_fitting.FitIndustryModels(
        Samples(), split, HEAD_CONTRACTS, model_fitting.RidgeFittingParameters(1, 1.0),
        compute_threads=1, progress=Progress(), industry_snapshot=industry,
    )

    assert len(attempts) == (4 if second_head_full else 3)
    assert len(result[0].models) == len(result[0].failures) == len(result[1].models) == 1
    assert len(result[1].failures) == int(second_head_full)
    for contract in HEAD_CONTRACTS:
        total = 2 if contract is HEAD_CONTRACTS[0] or second_head_full else 1
        finished = TrainingFitCounts(total, total, 1, total - 1)
        updates = [event for event in events if event.stage == "model_fit" and event.strategy is contract.strategy]
        assert all(event.fit_counts is not None for event in updates)
        for event in updates:
            if event.fit_detail is not None:
                member = next(item for item in industry.members if item.reference.industry_id == event.fit_detail.industry)
                assert event.fit_detail.industry_name == member.name
        assert all((event.completed_units, event.total_units, event.produced_units)
                   == (event.fit_counts.processed, event.fit_counts.total, event.fit_counts.fitted) for event in updates)
        processed = [event.fit_counts.processed for event in updates]
        assert processed == sorted(processed) and set(processed) == set(range(total + 1))
        assert updates[0].fit_counts == TrainingFitCounts(0, total, 0, 0)
        assert updates[-1].state == "completed" and updates[-1].fit_counts == finished
        skipped = [event for event in updates if event.fit_detail and event.fit_detail.phase == "skipped"]
        assert len(skipped) == total - 1
        assert all(event.fit_counts.skipped == 1 for event in skipped)
        assert all(event.fit_counts == finished for event in updates if event.validation is not None)
    d25_finished_second = next(event for event in events if event.strategy is HEAD_CONTRACTS[1].strategy
                               and event.fit_detail and event.fit_detail.industry == _SECOND
                               and event.fit_detail.phase == "completed")
    assert d25_finished_second.fit_counts == TrainingFitCounts(1, 2 if second_head_full else 1, 1, 0)


def test_final_refit_retains_the_frozen_industry_name_and_actual_task_counts(monkeypatch):
    original = FittedIndustryModel(
        _FIRST, TrainedRidgeModelSnapshot((0.0,) * 6, (1.0,) * 6, 0.0, 1.0, 0.0, (0.0,) * 6),
        10, CalibrationResult(2, 2, 1.0, 0.0, 1.0),
    )
    diagnostic = IndustryFitResult((original,), (), PredictionMetrics(0, 0, 0, 0, 0, None, None, None, 0), (), 10, 0)
    industry = CurrentIndustrySnapshotFixture(("600001",))
    events = []

    class Samples:
        def final_fit_counts(self, _contract, _plan):
            return (FinalFitIndustryCounts(_FIRST, 10, 2),)

    class Progress:
        def publish(self, event):
            events.append(event)

    def refit(_samples, _counts, _contract, _plan, _parameters, previous, heartbeat, _threads):
        heartbeat()
        return previous

    monkeypatch.setattr(model_fitting, "_refit_industry", refit)
    result = model_fitting.RefitIndustryModels(
        Samples(), HEAD_CONTRACTS[0], FinalFitDatePlan(date(2026, 1, 3), 1, (date(2026, 1, 1), date(2026, 1, 2))),
        diagnostic, model_fitting.RidgeFittingParameters(1, 1.0),
        compute_threads=1, progress=Progress(), industry_snapshot=industry,
    )
    assert result == (original,)
    assert all(event.stage == "final_model_refit" for event in events)
    assert events[0].fit_counts == TrainingFitCounts(0, 1, 0, 0)
    assert events[-1].fit_counts == TrainingFitCounts(1, 1, 1, 0)
    assert all(event.fit_detail.industry_name == "货币金融服务" for event in events if event.fit_detail is not None)


@pytest.mark.parametrize("stage", ("model_fit", "final_model_refit"))
def test_v1_reports_one_actual_shared_task_per_head(stage):
    events = []

    class Progress:
        def publish(self, event):
            events.append(event)

    for state in ("started", "running", "completed"):
        learned_rank_training._progress(Progress(), stage, state, HEAD_CONTRACTS[0].strategy)
    assert all(event.count_unit == "tasks" for event in events)
    assert [event.fit_counts for event in events] == [
        TrainingFitCounts(0, 1, 0, 0), TrainingFitCounts(0, 1, 0, 0), TrainingFitCounts(1, 1, 1, 0),
    ]


@pytest.mark.parametrize("values", ((-1, 1, 0, 0), (2, 1, 2, 0), (1, 2, 0, 0), (1, 2, 1, 1), (True, 1, 1, 0)))
def test_fit_counts_reject_impossible_work_completion(values):
    with pytest.raises(ValueError, match="training_fit_counts_invalid"):
        TrainingFitCounts(*values)


def test_fit_counts_cannot_disagree_with_the_progress_units():
    with pytest.raises(ValueError, match="training progress counts"):
        TomorrowTrainingProgress("model_fit", "running", 0, 2, fit_counts=TrainingFitCounts(1, 2, 1, 0))
