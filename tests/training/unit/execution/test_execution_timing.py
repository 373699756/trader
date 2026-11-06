"""Reservation failures, bounded backpressure and exact timing with an injected clock."""

from tests.helpers.training.training_resources import TrainingSampleSettingsFixture

from tests.helpers.training.training_resources import TrainingResourceSettingsFixture

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from datetime import date
from threading import Barrier, Event, get_native_id, local
import time

import pytest

from trader.download.domain.history_revision import HistoryTrainingRow
from trader.training.application.resources import TrainingExecutionTiming, TrainingFanoutSettings, TrainingProfileTiming, TrainingResourceTiming
from trader.training.application.selected_training import RunSelectedTrainingTasks, SelectedTrainingTask
from trader.training.application.selected_training import _TrainingQueue
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


class _ThreadClock(local):
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


@pytest.mark.parametrize("failure", (None, ValueError("fixture_failed"), InterruptedError("fixture_cancelled")))
def test_task_thread_cpu_is_distinct_from_wall_time_and_final_on_all_exits(failure):
    wall, cpu = _Clock(), _Clock()
    measured = TrainingExecutionMeasurements(("v1", "v5"), wall, thread_time=cpu, native_id=lambda: 731)
    before = measured.snapshot()
    outcome = pytest.raises(type(failure)) if failure is not None else nullcontext()
    with outcome as caught, measured.task("v1"):
        wall.advance(20)
        cpu.advance(2.5)
        running = measured.snapshot().profiles[0]
        assert running.native_id == 731
        assert running.thread_cpu_seconds is running.finished_after_seconds is None
        if failure is not None:
            raise failure
    if failure is not None:
        assert caught.value is failure
    first, unstarted = measured.snapshot().profiles
    assert first.finished_after_seconds - first.started_after_seconds == 20
    assert first.native_id == 731 and first.thread_cpu_seconds == 2.5
    assert unstarted.native_id is unstarted.thread_cpu_seconds is None
    assert before.profiles[0].native_id is before.profiles[0].thread_cpu_seconds is None
    projected = ProjectTrainingExecutionTiming(measured.snapshot())
    assert projected["profiles"][0]["native_id"] == 731
    assert projected["profiles"][0]["thread_cpu_seconds"] == 2.5
    assert projected["profiles"][1]["thread_cpu_seconds"] is None
    assert projected["thread_cpu_scope"] == (
        "calling_thread_only; excludes_other_threads_and_native_workers; task_values_final_on_exit"
    )


def test_source_thread_cpu_accumulates_completed_and_failed_read_scopes():
    wall, cpu = _Clock(), _Clock()
    measured = TrainingExecutionMeasurements(("v5",), wall, thread_time=cpu)
    with measured.read_source():
        wall.advance(10)
        cpu.advance(1.25)
    failure = InterruptedError("fixture_read_cancelled")
    with pytest.raises(InterruptedError) as caught, measured.read_source():
        wall.advance(4)
        cpu.advance(0.5)
        raise failure
    assert caught.value is failure
    timing = measured.snapshot()
    assert timing.source_read_seconds == 14
    assert timing.source_read_thread_cpu_seconds == 1.75
    assert ProjectTrainingExecutionTiming(timing)["source_read_thread_cpu_seconds"] == 1.75
    assert timing.profiles[0].native_id is timing.profiles[0].thread_cpu_seconds is None


@pytest.mark.parametrize("failure", (None, ValueError("sample_failed"), InterruptedError("sample_cancelled")))
def test_sample_phase_preserves_wall_cpu_counts_and_partial_failures(failure):
    wall, cpu = _Clock(), _Clock()
    measured = TrainingExecutionMeasurements(("v1", "v5"), wall, thread_time=cpu)
    outcome = pytest.raises(type(failure)) if failure is not None else nullcontext()
    with outcome, measured.sample_build("v1"):
        running = measured.snapshot().profiles[0].sample_build
        assert running.count == running.inflight_count == 1
        assert running.wall_seconds is running.thread_cpu_seconds is None
        wall.advance(7)
        cpu.advance(2)
        if failure is not None:
            raise failure
    observed, untouched = measured.snapshot().profiles
    assert observed.sample_build.count == 1
    assert observed.sample_build.completed_count == int(failure is None)
    assert observed.sample_build.inflight_count == 0
    assert observed.sample_build.wall_seconds == 7
    assert observed.sample_build.thread_cpu_seconds == 2
    assert observed.sample_build.first_started_after_seconds == 0
    assert observed.sample_build.last_finished_after_seconds == 7
    assert untouched.sample_build.count == 0
    assert untouched.sample_build.wall_seconds is untouched.sample_build.thread_cpu_seconds is None
    report = ProjectTrainingExecutionTiming(measured.snapshot())
    assert report["profiles"][0]["sample_build"]["wall_seconds"] == 7
    assert "do_not_add" in report["overlap_scope"]
    assert "input_wait_and_sqlite" in report["sample_build_scope"]


def test_zero_duration_sample_phase_is_distinct_from_an_unentered_or_reused_phase():
    wall, cpu = _Clock(), _Clock()
    measured = TrainingExecutionMeasurements(("v1", "v5"), wall, thread_time=cpu)
    with measured.sample_build("v1"):
        pass
    zero, untouched = measured.snapshot().profiles
    assert zero.sample_build.count == zero.sample_build.completed_count == 1
    assert zero.sample_build.wall_seconds == zero.sample_build.thread_cpu_seconds == 0
    assert untouched.sample_build.count == 0
    assert untouched.sample_build.wall_seconds is untouched.sample_build.thread_cpu_seconds is None


@pytest.mark.parametrize("reason", ("full", "empty"))
def test_queue_retry_scope_records_interruption_in_finally(reason):
    clock = _Clock()
    measured = TrainingExecutionMeasurements(("v5",), clock)
    with pytest.raises(KeyboardInterrupt), measured.wait_queue("v5", reason):
        clock.advance(3)
        raise KeyboardInterrupt
    observed = measured.snapshot().profiles[0]
    assert observed.queue_full_wait_count == int(reason == "full")
    assert observed.queue_empty_wait_count == int(reason == "empty")
    assert observed.queue_full_wait_seconds == (3 if reason == "full" else 0)
    assert observed.queue_empty_wait_seconds == (3 if reason == "empty" else 0)


def test_queue_peak_is_observed_under_mutex_and_excludes_sentinels(monkeypatch):
    class _Measurements(TrainingExecutionMeasurements):
        def queue_depth(self, profile, batches):
            assert queue.mutex.locked()
            super().queue_depth(profile, batches)

    measured = _Measurements(("v5",), time.monotonic)
    queue = _TrainingQueue(2, "v5", measured)
    monkeypatch.setattr(queue, "qsize", lambda: pytest.fail("approximate queue size used"))
    queue.put_nowait(None)
    assert measured.snapshot().profiles[0].queue_peak_batches == 0
    assert queue.get_nowait() is None
    row = HistoryTrainingRow("600000", date(2026, 1, 5), "main", "bank", None)
    queue.put_nowait((row,))
    queue.put_nowait((row,))
    assert queue.get_nowait() == (row,)
    assert queue.get_nowait() == (row,)
    observed = measured.snapshot().profiles[0]
    assert observed.queue_peak_batches == 2
    assert observed.first_batch_after_seconds is None


def test_worker_native_ids_and_thread_cpu_do_not_include_other_threads():
    wall, cpu = _Clock(), _ThreadClock()
    measured = TrainingExecutionMeasurements(("v1", "v5"), wall, thread_time=cpu)
    rendezvous = Barrier(2)

    def run(profile, seconds):
        with measured.task(profile):
            native_id = get_native_id()
            rendezvous.wait(timeout=5)
            cpu.advance(seconds)
        return native_id

    with measured.read_source(), ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(run, "v1", 2.0)
        second = workers.submit(run, "v5", 3.0)
        native_ids = first.result(timeout=5), second.result(timeout=5)
        wall.advance(7)
    timing = measured.snapshot()
    assert native_ids[0] != native_ids[1]
    assert tuple(item.native_id for item in timing.profiles) == native_ids
    assert tuple(item.thread_cpu_seconds for item in timing.profiles) == (2.0, 3.0)
    assert timing.source_read_seconds == 7
    assert timing.source_read_thread_cpu_seconds == 0


def test_direct_timing_construction_keeps_unobserved_cpu_and_native_ids_null():
    resource = TrainingResourceTiming(0.0, 0.0, 0, 0, 0)
    profile = TrainingProfileTiming("v1", None, None, 0.0, 0.0, resource, resource, 0.0)
    timing = TrainingExecutionTiming(0.0, 0, 0, (profile,))
    projected = ProjectTrainingExecutionTiming(timing)
    assert projected["source_read_thread_cpu_seconds"] is None
    assert projected["profiles"][0]["native_id"] is None
    assert projected["profiles"][0]["thread_cpu_seconds"] is None


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
    cpu_budget, memory_budget = TrainingCpuBudget(3, settings=TrainingResourceSettingsFixture().cpu), TrainingMemoryBudget(1024)
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
    budget = TrainingCpuBudget(3, settings=TrainingResourceSettingsFixture().cpu) if resource == "cpu" else TrainingMemoryBudget(1024)
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
    cpu_budget, memory_budget = TrainingCpuBudget(5, settings=TrainingResourceSettingsFixture().cpu), TrainingMemoryBudget(2048)
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
        @contextmanager
        def wait_queue(self, profile, reason):
            with super().wait_queue(profile, reason):
                if reason == "full":
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
                settings=TrainingResourceSettingsFixture().fanout,
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
    assert measured.snapshot().profiles[0].queue_full_wait_seconds == 5
    assert measured.snapshot().profiles[0].queue_full_wait_count >= 1
    assert measured.snapshot().profiles[0].queue_peak_batches == 2
    assert measured.snapshot().profiles[0].first_batch_after_seconds == 5
    assert measured.snapshot().source_read_seconds == 0

    reused = TrainingExecutionMeasurements(("v1",), clock)
    with ThreadPoolExecutor(max_workers=1) as workers:
        run = RunSelectedTrainingTasks(
            (SelectedTrainingTask("v1", frozenset(), lambda _rows: 42),),
            lambda _dates: pytest.fail("reused models opened a history stream"),
            workers,
            lambda: False,
            observer=reused,
            settings=TrainingResourceSettingsFixture().fanout,
        )
    assert run.history_scan_passes == run.history_rows_read == 0
    assert reused.snapshot().source_read_seconds == 0
    assert reused.snapshot().profiles[0].started_after_seconds is not None
    assert reused.snapshot().profiles[0].cpu.acquired_count == reused.snapshot().profiles[0].memory.acquired_count == 0
    assert reused.snapshot().profiles[0].first_batch_after_seconds is None
    assert reused.snapshot().profiles[0].queue_peak_batches == 0
    assert reused.snapshot().profiles[0].sample_build.count == 0


def test_empty_queue_wait_is_classified_only_after_empty_and_first_batch_is_data():
    clock = _Clock()
    waiting, release = Event(), Event()

    class _Measurements(TrainingExecutionMeasurements):
        @contextmanager
        def wait_queue(self, profile, reason):
            with super().wait_queue(profile, reason):
                if reason == "empty":
                    waiting.set()
                yield

    measured = _Measurements(("v5",), clock)
    day = date(2026, 1, 5)
    row = HistoryTrainingRow("600000", day, "main", "bank", None)

    def source():
        assert release.wait(3)
        yield row

    def run():
        with ThreadPoolExecutor(max_workers=1) as workers:
            return RunSelectedTrainingTasks(
                (SelectedTrainingTask("v5", frozenset((day,)), tuple),),
                lambda _dates: nullcontext(source()),
                workers,
                lambda: False,
                observer=measured,
                settings=TrainingResourceSettingsFixture().fanout,
            )

    with ThreadPoolExecutor(max_workers=1) as coordinator:
        future = coordinator.submit(run)
        try:
            assert waiting.wait(3)
            assert measured.snapshot().profiles[0].first_batch_after_seconds is None
            clock.advance(3)
        finally:
            release.set()
        result = future.result(timeout=5)
    assert result.outcomes[0].result == (row,)
    observed = measured.snapshot().profiles[0]
    assert observed.queue_empty_wait_count >= 1
    assert observed.queue_empty_wait_seconds == observed.queue_receive_seconds == 3
    assert observed.queue_full_wait_count == observed.queue_full_wait_seconds == 0
    assert observed.queue_peak_batches == 1
    assert observed.first_batch_after_seconds == 3


@pytest.mark.parametrize("reason", ("full", "empty"))
@pytest.mark.parametrize("failure", (InterruptedError, OSError))
def test_real_queue_wait_survives_cancel_or_source_failure_without_leaking_workers(reason, failure):
    clock = _Clock()
    waiting, release, cancelled = Event(), Event(), Event()

    class _Measurements(TrainingExecutionMeasurements):
        @contextmanager
        def wait_queue(self, profile, actual):
            with super().wait_queue(profile, actual):
                if actual == reason:
                    waiting.set()
                yield

    measured = _Measurements(("v5",), clock)
    day = date(2026, 1, 5)
    row = HistoryTrainingRow("600000", day, "main", "bank", None)

    def source():
        if reason == "empty":
            assert release.wait(3)
        else:
            yield row
            yield row
        raise failure("source_stopped")

    def consume(rows):
        if reason == "full":
            assert release.wait(3)
        return tuple(rows)

    def run():
        with ThreadPoolExecutor(max_workers=1) as workers:
            RunSelectedTrainingTasks(
                (SelectedTrainingTask("v5", frozenset((day,)), consume),),
                lambda _dates: nullcontext(source()),
                workers,
                cancelled.is_set,
                observer=measured,
                settings=TrainingFanoutSettings(1, 1),
            )

    with ThreadPoolExecutor(max_workers=1) as coordinator:
        future = coordinator.submit(run)
        try:
            assert waiting.wait(3)
            clock.advance(4)
            if failure is InterruptedError:
                cancelled.set()
        finally:
            release.set()
        with pytest.raises(failure):
            future.result(timeout=5)
    observed = measured.snapshot().profiles[0]
    assert observed.finished_after_seconds == 4
    if reason == "full":
        assert observed.queue_full_wait_count >= 1 and observed.queue_full_wait_seconds == 4
    else:
        assert observed.queue_empty_wait_count >= 1 and observed.queue_empty_wait_seconds == 4
    assert observed.sample_build.inflight_count == 0


@pytest.mark.parametrize("failure,expected_exit", ((None, 0), (ValueError, 1), (InterruptedError, 130)))
def test_entry_reports_finished_reservations_on_success_failure_and_cancel(
    tmp_path, monkeypatch, capsys, failure, expected_exit
):
    from scripts.core.reporting import ReadJsonReports
    from tests.helpers.shared.cold_start import _empty_config
    from tests.helpers.shared.configuration import SqlitePolicyFixture
    from trader.infra.settings import LoadRuntimeSettings
    from trader.infra.workers import BoundedExecutor
    from trader.training.application.selected_training import SelectedTrainingOutcome, SelectedTrainingRun, TrainingLocations
    from trader.training.entrypoints.selected_training import RunSelectedTraining
    from trader.training.infra.selected_training import SelectedTrainingAdapter
    from trader.training.application.learned_rank_training import V1TrainingResult

    def train(operations, _executor):
        request = operations.request
        sqlite_policy = operations.sqlite_policy
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
    cpu_budget, memory_budget = TrainingCpuBudget(3, settings=TrainingResourceSettingsFixture().cpu), TrainingMemoryBudget(1024)
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
        prepare_training=lambda request: SelectedTrainingAdapter(request, sqlite_policy=SqlitePolicyFixture()),
        fanout_settings=TrainingResourceSettingsFixture().fanout,
        sample_settings=TrainingSampleSettingsFixture(),
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
