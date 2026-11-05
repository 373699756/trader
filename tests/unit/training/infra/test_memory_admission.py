"""Mounted cgroup memory limits and atomic cancellation-safe fitting budgets."""

from tests.helpers.training_resources import TrainingResourceSettingsFixture

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
from threading import Event

import pytest

from trader.training.infra.cpu_admission import TrainingCpuBudget
from trader.training.infra.memory_admission import FittingMemoryCapacity, TrainingMemoryBudget

RESOURCE_SETTINGS = TrainingResourceSettingsFixture()
_GIB = 1024**3


def _memory_files(monkeypatch, files):
    monkeypatch.setattr(sys, "platform", "linux")

    def read(path, **_kwargs):
        if str(path) not in files:
            raise FileNotFoundError(str(path))
        value = files[str(path)]
        if isinstance(value, OSError):
            raise value
        return value

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "is_file", lambda path: str(path) in files)


@pytest.mark.parametrize("unified", (False, True))
@pytest.mark.parametrize("mount_root", ("/", "/tenant"))
def test_capacity_honors_mounted_controller_and_tightest_visible_parent(monkeypatch, unified, mount_root):
    membership = f"{mount_root.rstrip('/')}/user/job"
    files = {
        "/proc/meminfo": "MemAvailable: 8388608 kB\n",
        "/proc/self/cgroup": f"0::{membership}\n" if unified else f"7:memory:{membership}\n",
        "/proc/self/mountinfo": (
            f"1 0 0:1 {mount_root} /fixture rw - cgroup2 cgroup rw\n" if unified
            else f"1 0 0:1 {mount_root} /fixture rw - cgroup cgroup rw,memory\n"
        ),
    }
    name = "memory.max" if unified else "memory.limit_in_bytes"
    files.update({
        f"/fixture/user/job/{name}": str(4 * _GIB),
        f"/fixture/user/{name}": str(2 * _GIB),
        f"/fixture/{name}": "max" if unified else str(2**63 - 4096),
    })
    _memory_files(monkeypatch, files)
    assert FittingMemoryCapacity(RESOURCE_SETTINGS.memory) == _GIB // 2


@pytest.mark.parametrize("limit,expected", (("max", 3 * _GIB), (str(2 * _GIB), _GIB // 2), ("0", 1)))
def test_unified_high_limit_is_a_conservative_budget_bound(monkeypatch, limit, expected):
    _memory_files(monkeypatch, {
        "/proc/meminfo": "MemAvailable: 8388608 kB\n",
        "/proc/self/cgroup": "0::/job\n",
        "/proc/self/mountinfo": "1 0 0:1 / /fixture rw - cgroup2 cgroup rw\n",
        "/fixture/job/memory.max": "max",
        "/fixture/memory.high": limit,
    })
    assert FittingMemoryCapacity(RESOURCE_SETTINGS.memory) == expected


@pytest.mark.parametrize("failure", ("membership", "mount", "mapping", "malformed", "unreadable"))
def test_unknown_memory_constraints_fail_before_creating_a_fitting_budget(monkeypatch, failure):
    files = {
        "/proc/meminfo": "MemAvailable: 8388608 kB\n",
        "/proc/self/cgroup": "0::/job\n",
        "/proc/self/mountinfo": "1 0 0:1 / /fixture rw - cgroup2 cgroup rw\n",
        "/fixture/job/memory.max": "max",
    }
    if failure == "membership":
        del files["/proc/self/cgroup"]
    elif failure == "mount":
        files["/proc/self/mountinfo"] = "broken"
    elif failure == "mapping":
        files["/proc/self/mountinfo"] = "1 0 0:1 /another /fixture rw - cgroup2 cgroup rw\n"
    elif failure == "malformed":
        files["/fixture/job/memory.max"] = "unknown"
    else:
        files["/fixture/job/memory.max"] = PermissionError("fixture unreadable")
    _memory_files(monkeypatch, files)
    with pytest.raises(ValueError, match="training_memory_limit_unavailable") as caught:
        FittingMemoryCapacity(RESOURCE_SETTINGS.memory)
    assert isinstance(caught.value.__cause__, (OSError, ValueError, IndexError))


def test_non_linux_keeps_the_existing_capacity_without_reading_cgroup_files(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    def absent(_path, **_kwargs):
        raise FileNotFoundError("fixture no proc filesystem")

    monkeypatch.setattr(Path, "read_text", absent)
    assert FittingMemoryCapacity(RESOURCE_SETTINGS.memory) == 3 * _GIB


@pytest.mark.parametrize("value", (True, False, 1.5, float("nan"), float("inf"), 0, -1))
def test_budget_and_request_require_positive_integer_bytes(value):
    with pytest.raises(ValueError, match="training_memory_budget_invalid"):
        TrainingMemoryBudget(value)
    budget = TrainingMemoryBudget(100)
    with pytest.raises(ValueError, match="training_memory_request_exceeds_budget"), budget.reserve(value, lambda: None):
        pytest.fail("invalid byte request entered")
    with budget.reserve(100, lambda: None):
        pass


def test_cancelled_memory_wait_returns_the_already_held_cpu_reservation():
    cpu, memory = TrainingCpuBudget(3, settings=TrainingResourceSettingsFixture().cpu), TrainingMemoryBudget(100)
    waiting, cancelled = Event(), Event()

    def heartbeat():
        waiting.set()
        if cancelled.is_set():
            raise InterruptedError("fixture_cancelled")

    def fit():
        with cpu.reserve(2, lambda: None), memory.reserve(100, heartbeat):
            pytest.fail("memory was overcommitted")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with memory.reserve(100, lambda: None):
            future = pool.submit(fit)
            assert waiting.wait(2)
            cancelled.set()
            with pytest.raises(InterruptedError, match="fixture_cancelled"):
                future.result(timeout=2)
            with cpu.reserve(cpu.capacity, lambda: None):
                pass
    with cpu.reserve(cpu.capacity, lambda: None), memory.reserve(100, lambda: None):
        pass


@pytest.mark.parametrize("failure", (RuntimeError, InterruptedError))
def test_failure_inside_fitting_returns_both_full_budgets(failure):
    cpu, memory = TrainingCpuBudget(3, settings=TrainingResourceSettingsFixture().cpu), TrainingMemoryBudget(100)
    with pytest.raises(failure, match="fixture_fit_failed"):
        with cpu.reserve(2, lambda: None), memory.reserve(100, lambda: None):
            raise failure("fixture_fit_failed")
    with cpu.reserve(cpu.capacity, lambda: None), memory.reserve(100, lambda: None):
        pass
