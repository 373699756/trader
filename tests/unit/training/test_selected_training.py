"""Real worker fan-out, failure isolation, cancellation and memory admission."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from itertools import combinations
from threading import Barrier, Event

import pytest

from trader.download.domain.history_revision import HistoryTrainingRow
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS
from trader.training.application.selected_training import RunSelectedTrainingTasks, SelectedTrainingTask
from trader.training.infra.memory_admission import TrainingMemoryBudget

SELECTIONS = tuple(group for count in range(1, 5) for group in combinations(SCORING_PROFILE_IDS, count))
DAY = date(2026, 1, 5)


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
        result = RunSelectedTrainingTasks(tasks, read, executor, lambda: False)
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
        )
    assert result.history_scan_passes == result.history_rows_read == 0

    def fail(rows):
        next(rows)
        raise ValueError("fixture failure")

    rows = tuple(HistoryTrainingRow(f"{index:06d}", DAY, "main", "bank", None) for index in range(2000))
    with ThreadPoolExecutor(max_workers=2) as executor:
        result = RunSelectedTrainingTasks(
            (SelectedTrainingTask("v2", frozenset((DAY,)), fail), SelectedTrainingTask("v5", frozenset((DAY,)), tuple)),
            lambda _dates: iter(rows),
            executor,
            lambda: False,
        )
    assert result.outcomes[0].failure == "fixture failure"
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
            read,
            executor,
            cancelled.is_set,
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
    import json

    from tests.contract.test_cold_start import _empty_config
    from trader.bootstrap import ExecuteSelectedTraining
    from trader.infra.settings import LoadRuntimeSettings
    from trader.training.application.selected_training import SelectedTrainingOutcome, SelectedTrainingRun
    from trader.training.infra.profile.v1.training import V1TrainingResult

    executors = []

    def train(request, executor):
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
        immediate = capsys.readouterr().err
        assert (
            "V1训练 | 1/1 (100.00%) | 完成" in immediate
            if v1_status == "engineering_ready"
            else "V1训练 | 0/1 (0.00%) | 失败 | 错误 publication_failed" in immediate
        )
        assert "V5训练 | 0/1 (0.00%) | 失败 | 错误 dependency_missing" in immediate
        return SelectedTrainingRun(
            outcomes,
            1,
            123,
        )

    monkeypatch.setattr("trader.training.entrypoints.selected_training.TrainSelectedProfiles", train)
    runtime = LoadRuntimeSettings(_empty_config(tmp_path))
    assert ExecuteSelectedTraining(runtime, ("v1", "v5")) == 1
    assert not executors[0].status().running
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [item["profile"] for item in reports[:2]] == ["v1", "v5"]
    assert reports[-1]["worker_capacity"] == 2
    assert reports[-1]["history_scan_passes"] == 1
    assert reports[-1]["status"] == "failed"


def test_composed_cancellation_restores_signal_handler_and_stops_executor(tmp_path, monkeypatch, capsys):
    import signal

    from tests.contract.test_cold_start import _empty_config
    from trader.bootstrap import ExecuteSelectedTraining
    from trader.infra.settings import LoadRuntimeSettings

    previous = signal.getsignal(signal.SIGTERM)
    workers = []

    def train(request, executor):
        workers.append(executor._workers)
        signal.raise_signal(signal.SIGTERM)
        assert request.cancelled()
        raise InterruptedError("cancelled")

    monkeypatch.setattr("trader.training.entrypoints.selected_training.TrainSelectedProfiles", train)
    assert ExecuteSelectedTraining(LoadRuntimeSettings(_empty_config(tmp_path)), ("v1", "v5")) == 130
    assert signal.getsignal(signal.SIGTERM) == previous
    assert not workers[0].status().running
    assert '"status": "cancelled"' in capsys.readouterr().out
