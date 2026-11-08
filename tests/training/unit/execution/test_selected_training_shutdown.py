"""Selected training reports cleanup separately from committed profile results."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import date, timedelta
import signal
import threading

import pytest

from scripts.core.reporting import ReadJsonReports
from tests.helpers.shared.configuration import RuntimeSettingsFixture, SqlitePolicyFixture
from tests.helpers.training.current_industry import CurrentIndustrySnapshotFixture
from tests.helpers.training.history_training_due import HistoryTrainingExpectationFixture
from tests.helpers.training.training_resources import TrainingResourceSettingsFixture, TrainingSampleSettingsFixture
from trader.infra import signal_cancellation
from trader.infra.shutdown import ShutdownDeadline
from trader.infra.progress_heartbeat import ProgressHeartbeat
from trader.infra.workers import BoundedExecutor
from trader.training.application.learned_rank_training import V1TrainingResult
from trader.training.application.profile_training import HeadTrainingResult, TrainingRunResult
from trader.training.application.selected_training import RunSelectedTrainingTasks, SelectedTrainingOutcome, SelectedTrainingRun, SelectedTrainingTask, TrainingLocations
from trader.training.entrypoints import selected_training
from trader.training.entrypoints.tomorrow_training_progress import TrainingProgressLayout
from trader.training.domain.training_date_coverage import TrainingDateCoverage
from trader.recommendation.domain.publication.models import Strategy
from trader.infra.progress_format import DisplayWidth
from trader.training.infra.cpu_admission import TrainingCpuBudget
from trader.training.infra.memory_admission import TrainingMemoryBudget
from trader.training.infra.selected_training import SelectedTrainingAdapter


_CURRENT_INDUSTRY = CurrentIndustrySnapshotFixture(("000001", *(f"60000{number}" for number in range(6))))


@pytest.mark.parametrize("terminal_columns", (None, 92))
def test_selected_profiles_share_one_layout_and_report_brief_date_gaps_with_complete_json(
    tmp_path, monkeypatch, capsys, terminal_columns,
):
    resources = TrainingResourceSettingsFixture()
    width_reads = []
    layouts = []
    reporter = selected_training.StderrTomorrowTrainingProgress

    def layout():
        width_reads.append(True)
        return TrainingProgressLayout(96)

    def output(**options):
        layouts.append(options["layout"])
        return reporter(**options)

    required = tuple(date(2026, 10, 1) + timedelta(days=index) for index in range(10))
    coverage = TrainingDateCoverage(
        Strategy.TOMORROW, "final_fitting", "sample_index", date(2026, 11, 6), date(2026, 11, 5),
        required, required[:1],
    )
    v1 = V1TrainingResult(
        "blocked", "fixture", "a" * 64, 0, failure_reasons=("coverage_insufficient",), date_coverage=(coverage,),
    )
    head = HeadTrainingResult(
        Strategy.TOMORROW, "blocked", "complete_manifest", "fixture", "a" * 64,
        1, 1, "", "", 0, 0, 0, ("coverage_insufficient",), date_coverage=(coverage,),
    )
    standard = TrainingRunResult("fixture", "a" * 64, 0, (head,))

    def train(operations, _executor):
        outcomes = (SelectedTrainingOutcome("v1", v1), SelectedTrainingOutcome("v3", standard))
        for outcome in outcomes:
            operations.request.on_outcome(outcome)
        return SelectedTrainingRun(outcomes, 0, 0)

    monkeypatch.setattr(selected_training, "ReadTrainingProgressLayout", layout)
    monkeypatch.setattr(selected_training, "StderrTomorrowTrainingProgress", output)
    monkeypatch.setattr(selected_training, "TrainSelectedProfiles", train)
    monkeypatch.setattr(selected_training, "EvaluateAllHistoryTrainingDue", lambda *_args, **_options: ())
    pool = BoundedExecutor(worker_count=2, queue_capacity=0, thread_name_prefix="selected-layout-fixture")
    try:
        code = selected_training.RunSelectedTraining(
            RuntimeSettingsFixture(), ("v1", "v3"), executor=pool,
            memory_admission=TrainingMemoryBudget(1024), cpu_budget=TrainingCpuBudget(2, settings=resources.cpu),
            fanout_settings=resources.fanout, sample_settings=TrainingSampleSettingsFixture(),
            paths=TrainingLocations(tmp_path / "history", tmp_path / "models", tmp_path / "samples"),
            sqlite_policy=SqlitePolicyFixture(), terminal_columns=terminal_columns,
            load_expectation=HistoryTrainingExpectationFixture,
            prepare_training=lambda request: SelectedTrainingAdapter(
                request, sqlite_policy=SqlitePolicyFixture(), load_industry_snapshot=lambda _at: _CURRENT_INDUSTRY,
                clock=lambda: _CURRENT_INDUSTRY.observed_at,
            ),
        )
        captured = capsys.readouterr()
        assert code == 1 and len(width_reads) == int(terminal_columns is None)
        assert len(layouts) == 3 and all(value is layouts[0] for value in layouts)
        assert all(DisplayWidth(line) <= (terminal_columns or 96) for line in captured.err.splitlines())
        text = "".join(line.strip() for line in captured.err.splitlines())
        assert text.count("明日·最终拟合 有效1/10日") == 2
        assert required[-1].isoformat() not in text
        reports = ReadJsonReports(captured.out)
        assert reports[0]["date_coverage"][0]["missing_dates"] == [day.isoformat() for day in required[1:]]
        assert reports[1]["heads"][0]["date_coverage"] == reports[0]["date_coverage"]
    finally:
        pool.stop(wait=True, cancel_futures=True, deadline=ShutdownDeadline.start(2))


@pytest.mark.parametrize(("execution", "cleanup", "watchdog_fails"), (
    *((execution, cleanup, False) for execution in ("ready", "failure", "cancelled")
      for cleanup in ("complete", "incomplete", "exception")),
    ("ready", "complete", True),
    ("ready", "incomplete", True),
    ("ready", "exception", True),
    ("notification_scope_failure", "exception", False),
))
def test_final_result_follows_shutdown_and_preserves_training_outcome(tmp_path, monkeypatch, capsys, execution, cleanup, watchdog_fails):
    runtime = RuntimeSettingsFixture()
    resources = TrainingResourceSettingsFixture()
    pool = BoundedExecutor(worker_count=1, queue_capacity=0, thread_name_prefix="selected-shutdown-fixture")
    stop_workers = pool.stop
    controllers = []
    forced = []
    previous_controller = signal_cancellation.ShutdownSignalController
    previous_cancellation = signal_cancellation.ManualCancellation
    start_thread = threading.Thread.start
    publish_result = selected_training.StderrTomorrowTrainingProgress.publish_result
    before_shutdown = []
    stop_deadlines = []
    published = tmp_path / "committed-model.json"
    published.write_bytes(b"committed model fixture")

    def controller(**options):
        created = previous_controller(**options)
        controllers.append(created)
        return created

    @contextmanager
    def cancellation(**options):
        with previous_cancellation(**options, force_exit=forced.append) as scope:
            yield scope

    def start(thread):
        if watchdog_fails and thread.name == "trader-shutdown-watchdog":
            raise RuntimeError("private watchdog startup detail")
        return start_thread(thread)

    def notify(progress, *args):
        if execution == "notification_scope_failure":
            raise BrokenPipeError("private notification detail")
        return publish_result(progress, *args)

    def train(operations, executor):
        result = V1TrainingResult("engineering_ready", "committed-run", "a" * 64, 0)
        outcome = SelectedTrainingOutcome("v1", result)
        if execution == "notification_scope_failure":
            @contextmanager
            def broken_scope():
                yield
                raise OSError("private source scope detail")

            with broken_scope():
                return RunSelectedTrainingTasks(
                    (SelectedTrainingTask("v1", frozenset(), lambda _rows: result),),
                    lambda _dates: pytest.fail("committed result must not open history"),
                    executor, lambda: False, settings=resources.fanout,
                    on_outcome=operations.request.on_outcome,
                )
        operations.request.on_outcome(outcome)
        if execution == "cancelled":
            signal.raise_signal(signal.SIGTERM)
            raise RuntimeError("private cancelled work detail")
        if execution == "failure":
            raise ValueError("fixture_training_failed")
        return SelectedTrainingRun((outcome,), 1, 123)

    def stop(**options):
        before_shutdown.append(capsys.readouterr().out)
        assert '"schema_version": "selected_training_result"' not in before_shutdown[-1]
        deadline = options["deadline"]
        stop_deadlines.append(deadline)
        assert deadline is controllers[0].deadline
        assert options["wait"] and options["cancel_futures"]
        report = stop_workers(**options)
        assert report.completed
        if cleanup == "exception":
            raise OSError("private shutdown detail")
        return report if cleanup == "complete" else replace(report, completed=False, timed_out=True, detail="private pending resource detail")

    monkeypatch.setattr(signal_cancellation, "ShutdownSignalController", controller)
    monkeypatch.setattr(selected_training, "ManualCancellation", cancellation)
    monkeypatch.setattr(selected_training, "TrainSelectedProfiles", train)
    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(selected_training.StderrTomorrowTrainingProgress, "publish_result", notify)
    monkeypatch.setattr(pool, "stop", stop)
    try:
        code = selected_training.RunSelectedTraining(
            runtime, ("v1",), executor=pool, memory_admission=TrainingMemoryBudget(1024),
            cpu_budget=TrainingCpuBudget(2, settings=resources.cpu), fanout_settings=resources.fanout,
            sample_settings=TrainingSampleSettingsFixture(), paths=TrainingLocations(tmp_path / "history", tmp_path / "models", tmp_path / "samples"),
            sqlite_policy=SqlitePolicyFixture(),
            load_expectation=HistoryTrainingExpectationFixture,
            prepare_training=lambda request: SelectedTrainingAdapter(request, sqlite_policy=SqlitePolicyFixture(), load_industry_snapshot=lambda _observed: _CURRENT_INDUSTRY, clock=lambda: _CURRENT_INDUSTRY.observed_at),
        )
        reports = ReadJsonReports("".join(before_shutdown) + capsys.readouterr().out)
        summary = reports[-1]
        assert summary["schema_version"] == "selected_training_result"
        assert sum(item["schema_version"] == "selected_training_result" for item in reports) == 1
        assert code == (130 if execution == "cancelled" else int(execution != "ready" or cleanup != "complete" or watchdog_fails))
        assert summary["status"] == ("cancelled" if execution == "cancelled" else "failed" if code else "completed")
        assert summary["training_status"] == ("completed" if execution == "ready" else "cancelled" if execution == "cancelled" else "failed")
        assert summary["shutdown"]["completed"] is (cleanup == "complete")
        assert set(summary["shutdown"]) == {"name", "completed", "timed_out", "cancelled_count", "failure", "watchdog_started", "watchdog_failure"}
        assert summary["shutdown"]["watchdog_started"] is not watchdog_fails
        assert summary["shutdown"]["watchdog_failure"] == ("ShutdownWatchdogStartError" if watchdog_fails else None)
        if cleanup == "exception":
            assert summary["shutdown"]["failure"] == "OSError"
        assert reports[0]["status"] == "engineering_ready"
        assert reports[0]["run_id"] == "committed-run"
        assert summary["results"][0]["exit_code"] == 0
        if execution == "notification_scope_failure":
            assert summary["failure"] == "OSError"
            expected_issue = {"stage": "outcome_notification", "error_type": "BrokenPipeError", "cause_type": None}
            assert reports[0]["delivery_issues"] == [expected_issue]
            assert summary["results"][0]["delivery_issues"] == [expected_issue]
        elif execution != "ready":
            assert summary["failure"] == ("RuntimeError" if execution == "cancelled" else "fixture_training_failed")
        assert published.read_bytes() == b"committed model fixture"
        assert len(stop_deadlines) == 1 and not pool.status().running
        assert forced == ([2] if watchdog_fails and cleanup != "complete" else [])
        assert "private" not in str(reports)
    finally:
        for current in controllers:
            current.mark_completed()
        stop_workers(cancel_futures=True, deadline=ShutdownDeadline.start(2))


@pytest.mark.parametrize(("progress_failure", "run_status"), (
    ("io", "ready"), ("incomplete", "ready"), ("cancelled", "ready"),
    ("io", "failed"), ("io", "cancelled"),
))
def test_returned_run_keeps_training_status_when_progress_cleanup_fails(tmp_path, monkeypatch, capsys, progress_failure, run_status):
    runtime = RuntimeSettingsFixture()
    resources = TrainingResourceSettingsFixture()
    pool = BoundedExecutor(worker_count=1, queue_capacity=0, thread_name_prefix="selected-progress-shutdown-fixture")
    previous_cancellation = signal_cancellation.ManualCancellation
    stop_progress = ProgressHeartbeat.stop
    scopes = []

    @contextmanager
    def cancellation(**options):
        with previous_cancellation(**options, force_exit=lambda _code: None) as scope:
            scopes.append(scope)
            yield scope

    def train(operations, _executor):
        result = V1TrainingResult("engineering_ready" if run_status == "ready" else "failed", "retained-run", "a" * 64, 0)
        outcome = SelectedTrainingOutcome("v1", None, "cancelled", True) if run_status == "cancelled" else SelectedTrainingOutcome("v1", result)
        operations.request.on_outcome(outcome)
        return SelectedTrainingRun((outcome,), 1, 123)

    def stop(heartbeat, deadline):
        report = stop_progress(heartbeat, deadline)
        assert report.completed
        if progress_failure == "io":
            heartbeat._error = BrokenPipeError("private progress detail")
            return report
        if progress_failure == "cancelled":
            signal.raise_signal(signal.SIGTERM)
        # The real thread has closed; the fixture separately exercises an incomplete close report.
        return replace(report, completed=False, timed_out=True, detail="fixture progress remains pending")

    monkeypatch.setattr(selected_training, "ManualCancellation", cancellation)
    monkeypatch.setattr(selected_training, "TrainSelectedProfiles", train)
    monkeypatch.setattr(ProgressHeartbeat, "stop", stop)
    try:
        code = selected_training.RunSelectedTraining(
            runtime, ("v1",), executor=pool, memory_admission=TrainingMemoryBudget(1024),
            cpu_budget=TrainingCpuBudget(2, settings=resources.cpu), fanout_settings=resources.fanout,
            sample_settings=TrainingSampleSettingsFixture(), paths=TrainingLocations(tmp_path / "history", tmp_path / "models", tmp_path / "samples"),
            sqlite_policy=SqlitePolicyFixture(),
            load_expectation=HistoryTrainingExpectationFixture,
            prepare_training=lambda request: SelectedTrainingAdapter(request, sqlite_policy=SqlitePolicyFixture(), load_industry_snapshot=lambda _observed: _CURRENT_INDUSTRY, clock=lambda: _CURRENT_INDUSTRY.observed_at),
        )
        reports = ReadJsonReports(capsys.readouterr().out)
        summary = reports[-1]
        assert summary["training_status"] == ("completed" if run_status == "ready" else run_status)
        assert code == (130 if progress_failure == "cancelled" or run_status == "cancelled" else 1)
        assert summary["status"] == ("cancelled" if code == 130 else "failed")
        assert summary["failure"] == ("BrokenPipeError" if progress_failure == "io" else "TimeoutError")
        assert summary["progress_shutdown"][0]["completed"] is (progress_failure == "io")
        assert summary["progress_shutdown"][0]["timed_out"] is (progress_failure != "io")
        assert summary["results"][0]["exit_code"] == (130 if run_status == "cancelled" else int(run_status != "ready"))
        assert reports[0]["status"] == ("engineering_ready" if run_status == "ready" else run_status)
        if run_status != "cancelled":
            assert reports[0]["run_id"] == "retained-run"
        assert summary["shutdown"]["completed"] and not pool.status().running
        assert "private" not in str(reports)
    finally:
        for scope in scopes:
            scope._controller.mark_completed()
        pool.stop(cancel_futures=True, deadline=ShutdownDeadline.start(2))
