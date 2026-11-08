"""Real worker fan-out, failure isolation, cancellation and memory admission."""

from tests.helpers.training.training_resources import TrainingResourceSettingsFixture

from concurrent.futures import CancelledError, ThreadPoolExecutor, wait
from contextlib import closing, contextmanager, nullcontext
from datetime import date
from itertools import combinations
from threading import Barrier, Event

import pytest

from scripts.core.reporting import ReadJsonReports
from trader.download.domain.history_revision import HistoryTrainingRow
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS
from trader.training.application.selected_training import RunSelectedTrainingTasks, SelectedTrainingTask
from trader.training.entrypoints.report_output import TrainingReportOutput
from trader.training.infra.memory_admission import TrainingMemoryBudget

SELECTIONS = tuple(group for count in range(1, 5) for group in combinations(SCORING_PROFILE_IDS, count))
DAY = date(2026, 1, 5)


def test_outcome_observer_failure_preserves_the_worker_result_and_reports_delivery_issue():
    result = object()

    def failed_observer(_outcome):
        raise BrokenPipeError("private observer detail")

    with ThreadPoolExecutor(max_workers=1) as executor:
        run = RunSelectedTrainingTasks(
            (SelectedTrainingTask("v3", frozenset(), lambda _rows: result),),
            lambda _dates: pytest.fail("no history should be read"),
            executor, lambda: False,
            settings=TrainingResourceSettingsFixture().fanout,
            on_outcome=failed_observer,
        )

    outcome = run.outcomes[0]
    assert outcome.result is result and outcome.failure is None and not outcome.cancelled
    assert outcome.delivery_issues[0].stage == "outcome_notification"
    assert outcome.delivery_issues[0].error_type == "BrokenPipeError"
    assert "private" not in str(outcome.delivery_issues)


@pytest.mark.parametrize("profiles", SELECTIONS)
def test_one_scan_and_all_selected_workers_can_fit_concurrently(profiles):
    scans = []
    barrier = Barrier(len(profiles))
    source = tuple(HistoryTrainingRow(f"{index:06d}", DAY, "main", "bank", None) for index in range(1200))

    def read(dates):
        scans.append(dates)
        yield from source

    def fit(rows):
        received = tuple(rows)
        barrier.wait(timeout=5)
        return received

    tasks = tuple(SelectedTrainingTask(profile, frozenset((DAY,)), fit) for profile in profiles)
    with ThreadPoolExecutor(max_workers=len(profiles)) as executor:
        result = RunSelectedTrainingTasks(tasks, lambda dates: closing(read(dates)), executor, lambda: False, settings=TrainingResourceSettingsFixture().fanout)
    assert len(scans) == result.history_scan_passes == 1
    assert result.history_rows_read == len(source)
    assert tuple(item.profile for item in result.outcomes) == profiles
    assert all(item.result == source for item in result.outcomes)


def test_cache_hits_read_no_history_and_partial_failure_does_not_block_other_profile():
    def cached(_rows):
        return 42

    with ThreadPoolExecutor(max_workers=2) as executor:
        result = RunSelectedTrainingTasks(
            (SelectedTrainingTask("v2", frozenset(), cached), SelectedTrainingTask("v5", frozenset(), cached)),
            lambda _dates: pytest.fail("cache hit scanned history"),
            executor,
            lambda: False,
            settings=TrainingResourceSettingsFixture().fanout,
        )
    assert result.history_scan_passes == result.history_rows_read == 0

    def fail(rows):
        next(rows)
        raise ValueError("fixture failure")

    rows = tuple(HistoryTrainingRow(f"{index:06d}", DAY, "main", "bank", None) for index in range(2000))
    with ThreadPoolExecutor(max_workers=2) as executor:
        result = RunSelectedTrainingTasks(
            (SelectedTrainingTask("v2", frozenset((DAY,)), fail), SelectedTrainingTask("v5", frozenset((DAY,)), tuple)),
            lambda _dates: nullcontext(iter(rows)),
            executor,
            lambda: False,
            settings=TrainingResourceSettingsFixture().fanout,
        )
    assert result.outcomes[0].failure == "ValueError"
    assert result.outcomes[0].diagnostic.causes[0].error_type == "ValueError"
    assert result.outcomes[1].result == rows


@pytest.mark.parametrize("terminal", ("success", "failure", "cancelled"))
def test_finished_profile_reports_once_before_other_profile_finishes(terminal):
    notified = Event()
    outcomes = []

    def fast(_rows):
        if terminal == "failure":
            raise ValueError("publication_failed")
        if terminal == "cancelled":
            raise InterruptedError("cancelled")
        return 42

    def slow(_rows):
        assert notified.wait(5), "V1 result was delayed until V5 completion"
        assert [item.profile for item in outcomes] == ["v1"]
        return 24

    def report(outcome):
        outcomes.append(outcome)
        if outcome.profile == "v1":
            notified.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        run = RunSelectedTrainingTasks(
            (SelectedTrainingTask("v1", frozenset(), fast), SelectedTrainingTask("v5", frozenset(), slow)),
            lambda _dates: pytest.fail("no history expected"),
            executor,
            lambda: False,
            on_outcome=report,
            settings=TrainingResourceSettingsFixture().fanout,
        )
    assert tuple(outcomes) == run.outcomes
    assert run.outcomes[1].result == 24
    assert run.outcomes[0].cancelled == (terminal == "cancelled")
    assert (
        run.outcomes[0].failure
        == {"success": None, "failure": "publication_failed", "cancelled": "cancelled"}[terminal]
    )


def test_cancellation_unblocks_bounded_producer_and_workers():
    cancelled = Event()

    def read(_dates):
        for index in range(2000):
            if index == 900:
                cancelled.set()
            yield HistoryTrainingRow(f"{index:06d}", DAY, "main", "bank", None)

    with ThreadPoolExecutor(max_workers=2) as executor, pytest.raises(InterruptedError):
        RunSelectedTrainingTasks(
            tuple(SelectedTrainingTask(profile, frozenset((DAY,)), tuple) for profile in ("v2", "v5")),
            lambda dates: closing(read(dates)),
            executor,
            cancelled.is_set,
            settings=TrainingResourceSettingsFixture().fanout,
        )


@pytest.mark.parametrize("terminal", ("failure", "success", "unexpected_error"))
def test_finished_consumers_stop_scan_and_close_source_before_cached_worker_finishes(terminal, monkeypatch):
    closed = Event()
    futures, retained, scanned = [], [], []

    def source():
        try:
            for index in range(2000):
                if index == 256:
                    done, pending = wait(futures[:2], timeout=5)
                    assert len(done) == 2 and not pending
                scanned.append(index)
                yield HistoryTrainingRow(f"{index:06d}", DAY, "main", "bank", None)
        finally:
            closed.set()

    @contextmanager
    def read(_dates):
        rows = source()
        retained.append(rows)
        with closing(rows):
            yield rows

    def finish(rows):
        next(rows)
        if terminal == "failure":
            raise ValueError("fixture_failure")
        if terminal == "unexpected_error":
            raise TypeError("fixture_programming_error")
        return 1

    def cached(_rows):
        assert closed.wait(5), "history remained open while waiting for cached fitting"
        return 42

    with ThreadPoolExecutor(max_workers=3) as executor:
        submit = executor.submit

        def capture(*args, **kwargs):
            future = submit(*args, **kwargs)
            futures.append(future)
            return future

        monkeypatch.setattr(executor, "submit", capture)
        expected = pytest.raises(TypeError, match="fixture_programming_error") if terminal == "unexpected_error" else nullcontext()
        with expected:
            result = RunSelectedTrainingTasks(
                (SelectedTrainingTask("v1", frozenset((DAY,)), finish),
                 SelectedTrainingTask("v2", frozenset((DAY,)), finish),
                 SelectedTrainingTask("v5", frozenset(), cached)),
                read,
                executor,
                lambda: False,
                settings=TrainingResourceSettingsFixture().fanout,
            )
    assert 256 <= len(scanned) <= 512
    assert retained[0].gi_frame is None
    assert all(future.done() for future in futures)
    if terminal != "unexpected_error":
        assert result.history_rows_read == len(scanned)
        assert result.outcomes[2].result == 42
        assert [item.failure for item in result.outcomes[:2]] == (
            ["fixture_failure"] * 2 if terminal == "failure" else [None] * 2
        )


@pytest.mark.parametrize("interruption", ("cancelled", "source_error"))
def test_interrupted_scan_closes_retained_iterator_and_workers(interruption):
    cancelled, closed = Event(), Event()
    retained, outcomes = [], []

    def source():
        try:
            for index in range(2000):
                if index == 300:
                    if interruption == "source_error":
                        raise OSError("fixture_read_error")
                    cancelled.set()
                yield HistoryTrainingRow(f"{index:06d}", DAY, "main", "bank", None)
        finally:
            closed.set()

    def read(_dates):
        rows = source()
        retained.append(rows)
        return closing(rows)

    expected = OSError if interruption == "source_error" else InterruptedError
    with ThreadPoolExecutor(max_workers=2) as executor, pytest.raises(expected):
        RunSelectedTrainingTasks(
            tuple(SelectedTrainingTask(profile, frozenset((DAY,)), tuple) for profile in ("v1", "v5")),
            read,
            executor,
            cancelled.is_set,
            on_outcome=outcomes.append,
            settings=TrainingResourceSettingsFixture().fanout,
        )
    assert closed.is_set() and retained[0].gi_frame is None
    assert len(outcomes) == 2 and all(outcome.cancelled for outcome in outcomes)


def test_cancelled_before_start_never_opens_history_or_schedules_work():
    with ThreadPoolExecutor(max_workers=1) as executor, pytest.raises(InterruptedError):
        RunSelectedTrainingTasks(
            (SelectedTrainingTask("v1", frozenset((DAY,)), lambda _rows: pytest.fail("work started")),),
            lambda _dates: pytest.fail("history opened"),
            executor,
            lambda: True,
            settings=TrainingResourceSettingsFixture().fanout,
        )


def test_atomic_memory_wait_cancel_release_and_oversize():
    budget = TrainingMemoryBudget(100)
    entered = Event()
    cancelled = Event()

    def heartbeat():
        entered.set()
        if cancelled.is_set():
            raise InterruptedError("cancelled")

    def waiting():
        with budget.reserve(60, heartbeat):
            pytest.fail("overcommitted memory")

    with ThreadPoolExecutor(max_workers=1) as executor:
        with budget.reserve(60, lambda: None):
            future = executor.submit(waiting)
            assert entered.wait(2)
            cancelled.set()
            with pytest.raises(InterruptedError):
                future.result(timeout=2)
    with budget.reserve(100, lambda: None):
        pass
    with pytest.raises(ValueError, match="exceeds_budget"), budget.reserve(101, lambda: None):
        pass


@pytest.mark.parametrize("v1_status", ("engineering_ready", "failed"))
def test_composed_command_owns_worker_shutdown_and_reports_independent_failure(
    tmp_path, monkeypatch, capsys, v1_status
):

    from tests.helpers.shared.cold_start import _empty_config
    from trader.bootstrap import ExecuteSelectedTraining
    from trader.infra.settings import LoadRuntimeSettings
    from trader.training.application.selected_training import SelectedTrainingOutcome, SelectedTrainingRun
    from trader.training.application.learned_rank_training import V1TrainingResult

    executors = []

    def train(operations, executor):
        request = operations.request
        executors.append(executor._workers)
        assert executor._workers.status().workers == 2
        future = executor.submit(
            lambda: V1TrainingResult(
                v1_status,
                "fixture",
                "a" * 64,
                0,
                failure_reasons=("publication_failed",) if v1_status == "failed" else (),
            )
        )
        outcomes = (
            SelectedTrainingOutcome("v1", future.result()),
            SelectedTrainingOutcome("v5", None, "dependency_missing"),
        )
        for outcome in outcomes:
            request.on_outcome(outcome)
        immediate = " ".join(capsys.readouterr().err.split())
        assert (
            "train·V1·结果汇总" in immediate and "| 完成 | 状态 engineering_ready" in immediate
            if v1_status == "engineering_ready"
            else "train·V1·结果汇总" in immediate and "| 失败 | 状态 failed；错误 publication_failed" in immediate
        )
        assert "train·V5·结果汇总" in immediate and "| 失败 | 状态 failed；错误 dependency_missing" in immediate
        return SelectedTrainingRun(
            outcomes,
            1,
            123,
        )

    monkeypatch.setattr("trader.training.entrypoints.selected_training.TrainSelectedProfiles", train)
    monkeypatch.setattr("trader.training.entrypoints.commands.ConfigureTrainingResources", lambda *_args: None)
    runtime = LoadRuntimeSettings(_empty_config(tmp_path))
    assert ExecuteSelectedTraining(runtime, ("v1", "v5"), report_output=TrainingReportOutput(json_output=True)) == 1
    assert not executors[0].status().running
    reports = ReadJsonReports(capsys.readouterr().out)
    assert [item["profile"] for item in reports[:2]] == ["v1", "v5"]
    assert reports[-1]["worker_capacity"] == 2
    assert reports[-1]["history_scan_passes"] == 1
    assert reports[-1]["status"] == "failed"


@pytest.mark.parametrize("reused,scans,rows", ((False, 1, 123), (True, 0, 0)))
def test_single_profile_projects_actual_scan_evidence(tmp_path, monkeypatch, capsys, reused, scans, rows):

    from tests.helpers.shared.cold_start import _empty_config
    from trader.bootstrap import ExecuteSelectedTraining
    from trader.infra.settings import LoadRuntimeSettings
    from trader.training.application.selected_training import SelectedTrainingOutcome, SelectedTrainingRun
    from trader.training.application.learned_rank_training import V1TrainingResult

    requests = []

    def train(operations, executor):
        request = operations.request
        requests.append(request)
        result = V1TrainingResult("engineering_ready", "fixture", "a" * 64, 0, reused=reused)
        return SelectedTrainingRun((SelectedTrainingOutcome("v1", result),), scans, rows)

    monkeypatch.setattr("trader.training.entrypoints.selected_training.TrainSelectedProfiles", train)
    monkeypatch.setattr("trader.training.entrypoints.commands.ConfigureTrainingResources", lambda *_args: None)
    assert ExecuteSelectedTraining(LoadRuntimeSettings(_empty_config(tmp_path)), ("v1",), report_output=TrainingReportOutput(json_output=True)) == 0
    output = capsys.readouterr().out
    assert '\n  "schema_version": ' in output
    reports = ReadJsonReports(output)
    assert len(reports) == 2
    assert reports[0]["reused"] is reused
    assert reports[1]["schema_version"] == "selected_training_result"
    assert reports[1]["profiles"] == ["v1"]
    assert reports[1]["status"] == "completed"
    assert reports[1]["history_scan_passes"] == scans
    assert reports[1]["history_rows_read"] == rows
    request, = requests
    assert reports[1]["fitting_cpu_capacity"] == request.cpu_budget.capacity
    assert reports[1]["native_threads"] == request.cpu_budget.native_threads
    assert reports[1]["profile_threads"] == {
        "v1": request.cpu_budget.profile_threads.v1,
        "v2": request.cpu_budget.profile_threads.v2,
        "v3": request.cpu_budget.profile_threads.v3,
    }
    assert reports[1]["memory_capacity_bytes"] == request.memory_capacity_bytes
    assert reports[1]["fanout"] == {
        "batch_rows": request.fanout_settings.batch_rows,
        "queue_batches": request.fanout_settings.queue_batches,
    }


@pytest.mark.parametrize("error", (InterruptedError, CancelledError, RuntimeError))
def test_composed_cancellation_restores_signal_handler_and_stops_executor(tmp_path, monkeypatch, capsys, error):
    import signal

    from tests.helpers.shared.cold_start import _empty_config
    from trader.bootstrap import ExecuteSelectedTraining
    from trader.infra.settings import LoadRuntimeSettings

    previous = signal.getsignal(signal.SIGTERM)
    workers = []

    def train(operations, executor):
        request = operations.request
        workers.append(executor._workers)
        signal.raise_signal(signal.SIGTERM)
        assert request.cancelled()
        raise error("cancelled")

    monkeypatch.setattr("trader.training.entrypoints.selected_training.TrainSelectedProfiles", train)
    monkeypatch.setattr("trader.training.entrypoints.commands.ConfigureTrainingResources", lambda *_args: None)
    assert ExecuteSelectedTraining(LoadRuntimeSettings(_empty_config(tmp_path)), ("v1", "v5"), report_output=TrainingReportOutput(json_output=True)) == 130
    assert signal.getsignal(signal.SIGTERM) == previous
    assert not workers[0].status().running
    assert '"status": "cancelled"' in capsys.readouterr().out
