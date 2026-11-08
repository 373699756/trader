"""Completed trading days, shared input and per-profile sample work remain distinct."""

from contextlib import closing
from dataclasses import replace
from datetime import date, timedelta
from unittest.mock import Mock

import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.training.profiles.v1.learned_rank_training import _History, _settings
from tests.helpers.training.training_resources import TrainingSampleSettingsFixture
from trader.download.domain.history_revision import HistoryTrainingRow
from trader.download.domain.baostock_daily import BaoStockCalendar
from trader.training.application.tomorrow_training import (
    ProductionTrainingWindow, TomorrowTrainingProgress, TrainingSampleDiagnostics, TrainingSampleProgress,
)
from trader.training.domain.production_diagnostic_dates import BuildProductionDiagnosticDatePlan
from trader.training.domain.v1.learned_rank import BuildLearnedRankDateSplit
from trader.training.entrypoints.tomorrow_training_progress import StderrTomorrowTrainingProgress
from trader.training.infra.profile.v1.samples import BuildLearnedRankSamples, LearnedRankSampleIndex, LearnedRankSampleStream
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.sample_builder import BuildTrainingSamples, TrainingSampleBuildRequest
from trader.training.infra.sample_index import SQLiteTrainingSampleIndex
from trader.training.infra.sample_progress import TrainingSampleProgressTracker
from trader.training.infra.selected_training import _SelectedTrainingSession


class _Capture:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


def test_rows_within_one_day_do_not_finish_it_and_missing_days_never_become_complete():
    days = tuple(date(2026, 1, 1) + timedelta(days=i) for i in range(4))
    tracker = TrainingSampleProgressTracker(frozenset(days), days[1], None, interval_rows=2)
    tracker.observe(days[0])
    tracker.observe(days[0])
    assert tracker.snapshot() == TrainingSampleProgress(0, 4, 2, 2)
    tracker.observe(days[2])
    assert tracker.snapshot().completed_days == 1
    assert tracker.snapshot(finished=True).completed_days == 2
    assert tracker.snapshot().phase == "generation"


@pytest.mark.parametrize("profile", (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE, V5_TRAINING_PROFILE))
@pytest.mark.parametrize("failure", (None, InterruptedError, ValueError))
def test_standard_builders_report_all_input_and_zero_sample_causes_without_false_completion(profile, failure):
    history = _History()
    history.dates = tuple(date(2024, 1, 1) + timedelta(days=i) for i in range(600))
    history.snapshot = replace(history.snapshot, calendar=BaoStockCalendar(history.dates),
                               source_cutoff=history.dates[-1], label_cutoff=history.dates[-2])
    plan = BuildProductionDiagnosticDatePlan(
        history.dates, source_cutoff=history.dates[-1], fit_sessions=61, early_stopping_sessions=20,
        history_sessions=profile.history_sessions, history_excludes_signal=profile.algorithm == "tcn",
    )
    window = ProductionTrainingWindow(plan, history.dates)
    dates = tuple(sorted(window.readable_dates))
    capture = _Capture()

    def rows():
        for number, day in enumerate(dates):
            if failure is not None and number == len(dates) - 1:
                raise failure("fixture_input_stopped")
            yield HistoryTrainingRow("600000", day, "main", None, None)

    index = Mock(spec=SQLiteTrainingSampleIndex)
    request = TrainingSampleBuildRequest(history, ("600000",), window, index, profile, capture)
    if failure is None:
        BuildTrainingSamples(request, aligned_rows=rows(), aligned_dates=window.readable_dates)
    else:
        with pytest.raises(failure):
            BuildTrainingSamples(request, aligned_rows=rows(), aligned_dates=window.readable_dates)
    events = [e for e in capture.events if e.sample_progress is not None]
    assert all(e.stage == "history_conversion" and e.produced_units == 0 for e in events)
    assert events[1].sample_progress.completed_days == 1
    assert events[1].sample_progress.processed_rows == 2
    assert events[1].sample_diagnostics.missing_prices == 2
    assert any(e.sample_progress.phase == "warmup" for e in events)
    if failure is None:
        assert events[-1].state == "completed"
        assert events[-1].sample_progress.completed_days == len(dates)
        assert events[-1].sample_progress.phase == "generation"
        assert index.prepare_for_model_fitting.call_count == 1
    else:
        assert not any(e.state == "completed" for e in events)
        assert all(e.sample_progress.completed_days < len(dates) for e in events)


def test_v1_reports_complete_days_and_real_generated_samples(tmp_path):
    history, settings, capture = _History(), _settings(), _Capture()
    splits = tuple(BuildLearnedRankDateSplit(history.dates, settings, maturity_sessions=n) for n in (1, 5))
    with closing(LearnedRankSampleIndex(
        tmp_path / "samples.sqlite3", sqlite_policy=SqlitePolicyFixture(),
        sample_settings=TrainingSampleSettingsFixture(),
    )) as index:
        counts = BuildLearnedRankSamples(
            history, splits, index, history_sessions=settings.history_sessions,
            stream=LearnedRankSampleStream(capture),
        )
    final = capture.events[-1]
    assert final.state == "completed" and final.sample_progress.completed_days == len(history.dates)
    assert final.sample_progress.processed_rows == counts.source_rows
    assert final.produced_units == counts.samples > 0
    assert any(e.sample_progress.pending_samples > 0 for e in capture.events)
    assert not any(e.state == "running" and e.completed_units == e.total_units for e in capture.events)


@pytest.mark.parametrize("failure", (InterruptedError, ValueError))
def test_v1_interrupted_input_keeps_partial_progress(tmp_path, failure):
    history, settings, capture = _History(), _settings(), _Capture()
    splits = tuple(BuildLearnedRankDateSplit(history.dates, settings, maturity_sessions=n) for n in (1, 5))

    def rows():
        for row in history.iter_training_rows(frozenset(history.dates)):
            if row.trade_date == history.dates[-1]:
                raise failure("fixture_input_stopped")
            yield row

    with closing(LearnedRankSampleIndex(
        tmp_path / "samples.sqlite3", sqlite_policy=SqlitePolicyFixture(),
        sample_settings=TrainingSampleSettingsFixture(),
    )) as index, pytest.raises(failure):
        BuildLearnedRankSamples(
            history, splits, index, history_sessions=settings.history_sessions,
            stream=LearnedRankSampleStream(capture, aligned_rows=rows()),
        )
    assert not any(e.state == "completed" for e in capture.events)
    assert 0 < capture.events[-1].completed_units < capture.events[-1].total_units


def test_exhausted_input_with_missing_days_preserves_the_gap(capsys):
    output = StderrTomorrowTrainingProgress(monotonic=lambda: 0)
    value = TrainingSampleProgress(2, 3, 50, phase="generation")
    output.publish(TomorrowTrainingProgress("history_conversion", "completed", 2, 3, sample_progress=value))
    text = "".join(line.strip() for line in capsys.readouterr().err.splitlines())
    assert "2/3交易日 (66.6%)" in text and "输入缺日 1" in text and "100.0%" not in text


def test_shared_scan_reports_preceding_days_to_every_profile_and_does_not_fake_eof():
    days = tuple(date(2026, 1, 1) + timedelta(days=i) for i in range(3))
    captures = (_Capture(), _Capture())
    request = Mock(progresses=captures)
    session = _SelectedTrainingSession(request, Mock(), Mock(), Mock())

    def interrupted():
        for day in days[:2]:
            yield HistoryTrainingRow("600000", day, "main", None, None)
        raise InterruptedError("fixture_input_stopped")

    with pytest.raises(InterruptedError):
        list(session._observe_input(interrupted(), frozenset(days)))
    assert captures[0].events == captures[1].events
    assert captures[0].events[-1].input_progress == TrainingSampleProgress(1, 3, 2, phase="generation")


@pytest.mark.parametrize("profile", ("V1", "V2", "V3", "V5"))
def test_shared_progress_keeps_waiting_sample_visible_without_overwriting_fit(capsys, profile):
    clock = [0.0]
    output = StderrTomorrowTrainingProgress(monotonic=lambda: clock[0], command_label=f"train·{profile}")
    sample = TrainingSampleProgress(0, 100, 0)
    output.publish(TomorrowTrainingProgress("history_conversion", "started", 0, 100, sample_progress=sample))
    capsys.readouterr()
    clock[0] = 5
    output.publish(TomorrowTrainingProgress(
        "input_read", "running", 30, 2000, input_progress=TrainingSampleProgress(30, 2000, 120000),
    ))
    text = "".join(line.strip() for line in capsys.readouterr().err.splitlines())
    assert "本档输入处理 0/100交易日 (0.0%)" in text
    assert "共享扫描 30/2000交易日 (1.5%)" in text and "等待本档窗口" in text
    clock[0] = 6
    output.publish(TomorrowTrainingProgress("model_fit", "started", 0, 2))
    capsys.readouterr()
    clock[0] = 36
    output.publish(TomorrowTrainingProgress(
        "input_read", "running", 40, 2000, input_progress=TrainingSampleProgress(40, 2000, 160000),
    ))
    assert capsys.readouterr().err == ""
    output._emit_heartbeat(36)
    text = "".join(line.strip() for line in capsys.readouterr().err.splitlines())
    assert "拟合" in text and "未推进 00:00:30" in text and "共享扫描" not in text


def test_same_day_rows_are_progress_and_unfinished_percentage_never_rounds_to_100(capsys):
    clock = [0.0]
    output = StderrTomorrowTrainingProgress(monotonic=lambda: clock[0])
    value = TrainingSampleProgress(1999, 2000, 1, phase="generation", pending_samples=4)
    event = TomorrowTrainingProgress(
        "history_conversion", "running", 1999, 2000, sample_progress=value,
        sample_diagnostics=TrainingSampleDiagnostics(2, 3, 4, 5, 6),
    )
    output.publish(event)
    text = "".join(line.strip() for line in capsys.readouterr().err.splitlines())
    assert "99.9%" in text and "100.0%" not in text and "候选待标签 4" in text
    assert "缺价格3 缺行业2 窗口不足4 特征无效5 标签不足6" in text
    clock[0] = 29
    output.publish(replace(event, sample_progress=replace(value, processed_rows=4096)))
    capsys.readouterr()
    output._emit_heartbeat(59)
    assert "未推进 00:00:30" in "".join(line.strip() for line in capsys.readouterr().err.splitlines())


@pytest.mark.parametrize("changes", ({"completed_days": True}, {"completed_days": 3}, {"warmup_rows": 2}, {"pending_samples": -1}))
def test_sample_progress_rejects_impossible_counts(changes):
    with pytest.raises(ValueError, match="training_sample_progress_invalid"):
        TrainingSampleProgress(**({"completed_days": 0, "total_days": 2, "processed_rows": 1} | changes))
