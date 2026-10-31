"""Shared CPU admission with real worker/native limits and bounded cancellation."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from itertools import combinations
from pathlib import Path
from threading import Barrier, Event, Lock

import numpy as np
import pytest
from threadpoolctl import threadpool_info

from trader.download.domain.history_revision import HistoryTrainingRow
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS
from trader.training.application.selected_training import RunSelectedTrainingTasks, SelectedTrainingTask
from trader.training.infra.cpu_admission import AvailableTrainingCpus, TrainingCpuBudget


SELECTIONS = tuple(group for count in range(1, 5) for group in combinations(SCORING_PROFILE_IDS, count))


@pytest.mark.parametrize("profiles", SELECTIONS)
@pytest.mark.parametrize("cpus", (1, 2, 4))
def test_small_job_capacity_keeps_one_stream_and_every_selected_profile_completes(profiles, cpus):
    budget = TrainingCpuBudget(cpus)
    day = date(2026, 1, 5)
    source = tuple(HistoryTrainingRow(f"{index:06d}", day, "main", "bank", None) for index in range(1200))
    scans, active, maximum = [], 0, 0
    lock = Lock()

    def read(dates):
        scans.append(dates)
        yield from source

    def fit(rows):
        nonlocal active, maximum
        received = tuple(rows)
        with budget.reserve(4, lambda: None) as threads:
            with lock:
                active += threads
                maximum = max(active, maximum)
            try:
                assert received == source
                return len(received)
            finally:
                with lock:
                    active -= threads

    with ThreadPoolExecutor(max_workers=len(profiles)) as executor:
        result = RunSelectedTrainingTasks(
            tuple(SelectedTrainingTask(profile, frozenset((day,)), fit) for profile in profiles),
            read, executor, lambda: False,
        )
    assert len(scans) == result.history_scan_passes == 1
    assert all(outcome.result == len(source) for outcome in result.outcomes)
    assert 0 < maximum <= budget.capacity
    assert 0 < budget.peak_reserved_threads <= budget.capacity


def test_wait_is_cancellable_and_failure_returns_all_cpu_slots():
    budget = TrainingCpuBudget(3)
    waiting, cancelled = Event(), Event()

    def heartbeat():
        waiting.set()
        if cancelled.is_set():
            raise InterruptedError("cancelled")

    def wait():
        with budget.reserve(2, heartbeat):
            pytest.fail("CPU budget overcommitted")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(RuntimeError, match="fixture"), budget.reserve(2, lambda: None):
            future = pool.submit(wait)
            assert waiting.wait(2)
            cancelled.set()
            with pytest.raises(InterruptedError):
                future.result(timeout=2)
            raise RuntimeError("fixture")
    with budget.reserve(2, lambda: None) as threads:
        assert threads == 2
    with pytest.raises(InterruptedError), budget.reserve(1, heartbeat):
        pytest.fail("cancelled reservation acquired")


def test_sufficient_capacity_admits_parallel_profiles_and_charges_blas_baseline():
    budget = TrainingCpuBudget(5)
    entered = Barrier(2)

    def fit():
        with budget.reserve(1, lambda: None) as threads:
            assert threads == 1
            entered.wait(timeout=3)
            assert budget.peak_reserved_threads == 4

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = tuple(pool.submit(fit) for _ in range(2))
        for future in futures:
            future.result(timeout=5)


def test_loaded_blas_is_limited_in_workers_and_restored_after_failure():
    def pools():
        return {item["filepath"]: item["num_threads"] for item in threadpool_info() if item["user_api"] == "blas"}

    before = pools()
    assert before
    budget = TrainingCpuBudget(1)
    with pytest.raises(RuntimeError, match="fixture"), budget.native_runtime() as controller:
        def calculate():
            controller.limit(limits=budget.native_threads)
            with budget.reserve(2, lambda: None) as threads:
                assert threads == 1
                assert all(value == 1 for value in pools().values())
                return np.ones((32, 32)) @ np.ones((32, 32))

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(lambda _: calculate(), range(2)))
        assert all(np.array_equal(value, np.full((32, 32), 32)) for value in results)
        raise RuntimeError("fixture")
    assert pools() == before


@pytest.mark.parametrize("quota,expected", (("max 100000", 3), ("250000 100000", 2), ("50000 100000", 1), ("broken", 1)))
def test_capacity_honors_affinity_and_parent_cgroup_limits(monkeypatch, quota, expected):
    import trader.training.infra.cpu_admission as module

    files = {
        "/proc/self/cgroup": "0::/user/job\n",
        "/proc/self/mountinfo": "1 0 0:1 / /fixture rw - cgroup2 cgroup rw\n",
        "/fixture/user/job/cpu.max": "max 100000",
        "/fixture/user/cpu.max": quota,
        "/fixture/cpu.max": "300000 100000",
    }
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(module.os, "sched_getaffinity", lambda _pid: set(range(4)))
    monkeypatch.setattr(Path, "read_text", lambda path, **kwargs: files[str(path)])
    monkeypatch.setattr(Path, "is_file", lambda path: str(path) in files)
    assert AvailableTrainingCpus() == expected


@pytest.mark.parametrize("value", (0, -1, True, 1.5))
def test_invalid_capacity_and_requests_are_rejected(value):
    with pytest.raises(ValueError):
        TrainingCpuBudget(value)
    with pytest.raises(ValueError), TrainingCpuBudget(4).reserve(value, lambda: None):
        pass


def test_legacy_cpu_controller_uses_tightest_parent_quota(monkeypatch):
    import trader.training.infra.cpu_admission as module

    files = {
        "/proc/self/cgroup": "2:cpu,cpuacct:/user/job\n1:memory:/user/job\n",
        "/proc/self/mountinfo": "1 0 0:1 / /fixture rw - cgroup cgroup rw,cpu,cpuacct\n",
        "/fixture/user/job/cpu.cfs_quota_us": "200000",
        "/fixture/user/job/cpu.cfs_period_us": "100000",
        "/fixture/user/cpu.cfs_quota_us": "100000",
        "/fixture/user/cpu.cfs_period_us": "100000",
        "/fixture/cpu.cfs_quota_us": "-1",
    }
    monkeypatch.setattr(Path, "read_text", lambda path, **kwargs: files[str(path)])
    monkeypatch.setattr(Path, "is_file", lambda path: str(path) in files)
    assert module._linux_quota_capacities() == (2, 1)


def test_unmappable_cgroup_namespace_does_not_use_the_host_cpu_count(monkeypatch, caplog):
    import trader.training.infra.cpu_admission as module

    files = {
        "/proc/self/cgroup": "0::/\n",
        "/proc/self/mountinfo": "1 0 0:1 /container /fixture rw - cgroup2 cgroup rw\n",
    }
    monkeypatch.setattr(Path, "read_text", lambda path, **kwargs: files[str(path)])
    assert module._linux_quota_capacities() == (1,)
    assert "不可映射" in caplog.text
