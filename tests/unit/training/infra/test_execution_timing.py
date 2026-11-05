"""Reservation failures, bounded backpressure and exact timing with an injected clock."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from datetime import date
from threading import Event
import time

import pytest

from trader.download.domain.history_revision import HistoryTrainingRow
from trader.training.application.selected_training import RunSelectedTrainingTasks, SelectedTrainingTask
from trader.training.entrypoints.training_timing_projection import ProjectTrainingExecutionTiming
from trader.training.infra.cpu_admission import TrainingCpuBudget
from trader.training.infra.execution_timing import TrainingExecutionMeasurements
from trader.training.infra.memory_admission import TrainingMemoryBudget


class _Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class _CpuAdmission:
    def __init__(self, clock):
        self.clock = clock

    @contextmanager
    def reserve(self, requested, heartbeat):
        heartbeat()
        self.clock.advance(3)
        yield min(requested, 2)


class _MemoryAdmission:
    def __init__(self, clock, failure=None):
        self.clock = clock
        self.failure = failure

    @contextmanager
    def reserve(self, _required, heartbeat):
        heartbeat()
        self.clock.advance(5)
        if self.failure is not None:
            raise self.failure("fixture_wait_failed")
        yield


@pytest.mark.parametrize("failure", (None, ValueError, InterruptedError))
def test_wait_hold_overlap_and_failed_acquisition_have_distinct_timings(failure):
    clock = _Clock()
    measured = TrainingExecutionMeasurements(("v1", "v5"), clock)
    cpu = measured.cpu_admission("v1", _CpuAdmission(clock))
    memory = measured.memory_admission("v1", _MemoryAdmission(clock, failure))
    outcome = pytest.raises(failure, match="fixture_wait_failed") if failure else nullcontext()
    with outcome, measured.task("v1"), cpu.reserve(4, lambda: None) as granted:
        assert granted == 2
        with memory.reserve(512, lambda: None):
            clock.advance(7)

    first, unstarted = measured.snapshot().profiles
    assert first.started_after_seconds == 0
    assert first.finished_after_seconds == (15 if failure is None else 8)
    assert first.cpu.wait_seconds == 3
    assert first.cpu.hold_seconds == (12 if failure is None else 5)
    assert first.memory.wait_seconds == first.cpu_held_while_memory_wait_seconds == 5
    assert first.memory.hold_seconds == (7 if failure is None else 0)
    assert first.cpu.acquired_count == 1
    assert first.memory.acquired_count == int(failure is None)
    assert first.memory.failed_acquisitions == int(failure is not None)
    assert first.cpu.inflight_count == first.memory.inflight_count == 0
    assert unstarted.started_after_seconds is unstarted.finished_after_seconds is None
    assert ProjectTrainingExecutionTiming(measured.snapshot())["profiles"][1]["started_after_seconds"] is None


def test_failure_inside_a_reservation_preserves_cause_and_releases_the_real_budgets():
    cpu_budget, memory_budget = TrainingCpuBudget(3), TrainingMemoryBudget(1024)
    measured = TrainingExecutionMeasurements(("v3",), time.monotonic)
    failure = RuntimeError("fixture_fit_failed")
    with pytest.raises(RuntimeError) as caught:
        with (
            measured.cpu_admission("v3", cpu_budget).reserve(2, lambda: None),
            measured.memory_admission("v3", memory_budget).reserve(1024, lambda: None),
        ):
            raise failure
    assert caught.value is failure
    with cpu_budget.reserve(2, lambda: None), memory_budget.reserve(1024, lambda: None):
        pass
    timing = measured.snapshot()
    assert timing.peak_cpu_reservations == timing.peak_memory_reservations == 1
    assert timing.profiles[0].cpu.inflight_count == timing.profiles[0].memory.inflight_count == 0


@pytest.mark.parametrize("resource", ("cpu", "memory"))
def test_cancelled_real_budget_wait_is_measured_and_does_not_leak_capacity(resource):
    measured = TrainingExecutionMeasurements(("v2",), time.monotonic)
    budget = TrainingCpuBudget(3) if resource == "cpu" else TrainingMemoryBudget(1024)
    admission = measured.cpu_admission("v2", budget) if resource == "cpu" else measured.memory_admission("v2", budget)
    required = 2 if resource == "cpu" else 1024
    waiting, cancelled = Event(), Event()

    def heartbeat():
        waiting.set()
        if cancelled.is_set():
            raise InterruptedError("fixture_cancelled")

    def wait():
        with admission.reserve(required, heartbeat):
            pytest.fail("exhausted reservation was admitted")

    with ThreadPoolExecutor(max_workers=1) as executor:
        with budget.reserve(required, lambda: None):
            future = executor.submit(wait)
            assert waiting.wait(3)
            cancelled.set()
            with pytest.raises(InterruptedError, match="fixture_cancelled"):
                future.result(timeout=3)
    with budget.reserve(required, lambda: None):
        pass
    profile = measured.snapshot().profiles[0]
    timing = profile.cpu if resource == "cpu" else profile.memory
    assert timing.failed_acquisitions == 1
    assert timing.wait_seconds > 0
    assert timing.hold_seconds == timing.acquired_count == timing.inflight_count == 0


def test_parallel_reservation_scopes_and_snapshots_are_job_local():
    measured = TrainingExecutionMeasurements(("v1", "v5"), time.monotonic)
    cpu_budget, memory_budget = TrainingCpuBudget(5), TrainingMemoryBudget(2048)
    first_entered, second_entered = Event(), Event()

    def fit(profile, entered, other):
        with (
            measured.cpu_admission(profile, cpu_budget).reserve(1, lambda: None),
            measured.memory_admission(profile, memory_budget).reserve(1024, lambda: None),
        ):
            entered.set()
            assert other.wait(3)
            snapshot = measured.snapshot()
            assert snapshot.peak_cpu_reservations == snapshot.peak_memory_reservations == 2

    before = measured.snapshot()
    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = (
            executor.submit(fit, "v1", first_entered, second_entered),
            executor.submit(fit, "v5", second_entered, first_entered),
        )
        for job in jobs:
            job.result(timeout=5)
    assert before.profiles[0].cpu.acquired_count == 0
    assert all(item.cpu.acquired_count == item.memory.acquired_count == 1 for item in measured.snapshot().profiles)
    assert all(item.cpu.inflight_count == item.memory.inflight_count == 0 for item in measured.snapshot().profiles)


def test_full_queue_wait_is_separate_from_source_reading_and_reuse_reads_nothing():
    clock = _Clock()
    blocked, release = Event(), Event()

    class _QueueMeasurements(TrainingExecutionMeasurements):
        sends = 0

        @contextmanager
        def send_queue(self, profile):
            with super().send_queue(profile):
                self.sends += 1
                if self.sends == 3:
                    blocked.set()
                yield

    measured = _QueueMeasurements(("v5",), clock)
    day = date(2026, 1, 5)
    rows = tuple(HistoryTrainingRow(f"{index:06d}", day, "main", "bank", None) for index in range(2000))

    def consume(stream):
        assert release.wait(3)
        return tuple(stream)

    def run():
        with ThreadPoolExecutor(max_workers=1) as workers:
            return RunSelectedTrainingTasks(
                (SelectedTrainingTask("v5", frozenset((day,)), consume),),
                lambda _dates: nullcontext(iter(rows)),
                workers,
                lambda: False,
                observer=measured,
            )

    with ThreadPoolExecutor(max_workers=1) as coordinator:
        future = coordinator.submit(run)
        try:
            assert blocked.wait(3)
            assert not future.done()
            clock.advance(5)
        finally:
            release.set()
        result = future.result(timeout=5)
    assert result.history_scan_passes == 1 and result.outcomes[0].result == rows
    assert measured.snapshot().profiles[0].queue_send_seconds == 5
    assert measured.snapshot().source_read_seconds == 0

    reused = TrainingExecutionMeasurements(("v1",), clock)
    with ThreadPoolExecutor(max_workers=1) as workers:
        run = RunSelectedTrainingTasks(
            (SelectedTrainingTask("v1", frozenset(), lambda _rows: 42),),
            lambda _dates: pytest.fail("reused models opened a history stream"),
            workers,
            lambda: False,
            observer=reused,
        )
    assert run.history_scan_passes == run.history_rows_read == 0
    assert reused.snapshot().source_read_seconds == 0
    assert reused.snapshot().profiles[0].started_after_seconds is not None
    assert reused.snapshot().profiles[0].cpu.acquired_count == reused.snapshot().profiles[0].memory.acquired_count == 0


@pytest.mark.parametrize("failure,expected_exit", ((None, 0), (ValueError, 1), (InterruptedError, 130)))
def test_entry_reports_finished_reservations_on_success_failure_and_cancel(
    tmp_path, monkeypatch, capsys, failure, expected_exit
):
    from scripts.checks.reporting import ReadJsonReports
    from tests.contract.test_cold_start import _empty_config
    from tests.helpers.configuration import SqlitePolicyFixture
    from trader.infra.settings import LoadRuntimeSettings
    from trader.infra.workers import BoundedExecutor
    from trader.training.application.selected_training import SelectedTrainingOutcome, SelectedTrainingRun, TrainingLocations
    from trader.training.entrypoints.selected_training import RunSelectedTraining
    from trader.training.infra.profile.v1.training import V1TrainingResult

    def train(request, _executor, *, sqlite_policy):
        assert sqlite_policy == SqlitePolicyFixture()
        measured = request.measurements
        with (
            measured.task("v1"),
            measured.cpu_admission("v1", request.cpu_budget).reserve(2, lambda: None),
            measured.memory_admission("v1", request.memory_admission).reserve(1024, lambda: None),
        ):
            if failure is not None:
                raise failure("fixture_failed")
        result = V1TrainingResult("engineering_ready", "fixture", "a" * 64, 0)
        return SelectedTrainingRun((SelectedTrainingOutcome("v1", result),), 0, 0, timing=measured.snapshot())

    monkeypatch.setattr("trader.training.entrypoints.selected_training.TrainSelectedProfiles", train)
    cpu_budget, memory_budget = TrainingCpuBudget(3), TrainingMemoryBudget(1024)
    workers = BoundedExecutor(worker_count=1, queue_capacity=0, thread_name_prefix="timing-fixture")
    runtime = LoadRuntimeSettings(_empty_config(tmp_path))
    assert RunSelectedTraining(
        runtime,
        ("v1",),
        executor=workers,
        memory_admission=memory_budget,
        cpu_budget=cpu_budget,
        paths=TrainingLocations(tmp_path / "history", tmp_path / "models", tmp_path / "samples"),
        sqlite_policy=SqlitePolicyFixture(),
    ) == expected_exit
    report = ReadJsonReports(capsys.readouterr().out)[-1]
    assert report["status"] == {0: "completed", 1: "failed", 130: "cancelled"}[expected_exit]
    timing = report["timing"]["profiles"][0]
    assert timing["finished_after_seconds"] >= timing["started_after_seconds"] >= 0
    assert timing["cpu"]["acquired_count"] == timing["memory"]["acquired_count"] == 1
    assert timing["cpu"]["inflight_count"] == timing["memory"]["inflight_count"] == 0
    assert not workers.status().running
    with cpu_budget.reserve(2, lambda: None), memory_budget.reserve(1024, lambda: None):
        pass
