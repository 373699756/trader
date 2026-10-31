"""Job-owned CPU reservations and one lifetime for native thread limits."""

from __future__ import annotations

import os
import sys
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Condition

from threadpoolctl import ThreadpoolController

from trader.training.application.tomorrow_training import TOMORROW_TRAINING_COMPUTE_THREADS


class TrainingCpuBudget:
    def __init__(self, available_cpus: int) -> None:
        if type(available_cpus) is not int or available_cpus < 1:
            raise ValueError("training_cpu_capacity_invalid")
        self.available_cpus = available_cpus
        # Leave one CPU for history streaming, Python feature work and coordination.
        self.capacity = max(1, available_cpus - 1)
        self.native_threads = min(TOMORROW_TRAINING_COMPUTE_THREADS, self.capacity)
        self._available = self.capacity
        self._peak = 0
        self._condition = Condition()

    @property
    def peak_reserved_threads(self) -> int:
        with self._condition:
            return self._peak

    @contextmanager
    def reserve(self, requested_threads: int, heartbeat: Callable[[], None]) -> Iterator[int]:
        if type(requested_threads) is not int or requested_threads < 1:
            raise ValueError("training_cpu_request_invalid")
        threads = min(requested_threads, self.capacity)
        required = max(threads, self.native_threads)
        with self._condition:
            while self._available < required:
                heartbeat()
                self._condition.wait(timeout=0.25)
            heartbeat()
            self._available -= required
            self._peak = max(self._peak, self.capacity - self._available)
        try:
            yield threads
        finally:
            with self._condition:
                self._available += required
                self._condition.notify_all()

    @contextmanager
    def native_runtime(self) -> Iterator[ThreadpoolController]:
        """Enter after selected libraries load; restore only after all workers leave."""
        controller = ThreadpoolController()
        with controller.limit(limits=self.native_threads):
            yield controller


def AvailableTrainingCpus() -> int:
    """Honor process affinity and inherited Linux cgroup CPU quotas."""
    available = os.cpu_count() or 1
    if sys.platform == "linux":
        try:
            available = min(available, len(os.sched_getaffinity(0)))
        except OSError:
            logging.getLogger(__name__).warning("训练CPU亲和性不可确认，按1个CPU运行")
            available = 1
        for quota_capacity in _linux_quota_capacities():
            available = min(available, quota_capacity)
    elif sys.platform == "win32":
        available = min(available, _windows_affinity_capacity())
    return max(1, available)


def _windows_affinity_capacity() -> int:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
    kernel.GetProcessAffinityMask.restype = wintypes.BOOL
    process, system = ctypes.c_size_t(), ctypes.c_size_t()
    if not kernel.GetProcessAffinityMask(kernel.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(system)):
        return 1
    return max(1, process.value.bit_count())


def _linux_quota_capacities() -> tuple[int, ...]:
    try:
        memberships = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
        mounts = Path("/proc/self/mountinfo").read_text(encoding="ascii").splitlines()
        capacities = []
        for line in memberships:
            _identity, controllers, member = line.split(":", 2)
            unified = not controllers
            if not unified and "cpu" not in controllers.split(","):
                continue
            for mount in mounts:
                before, after = mount.split(" - ", 1)
                fields, filesystem = before.split(), after.split()
                if filesystem[0] != ("cgroup2" if unified else "cgroup"):
                    continue
                if not unified and "cpu" not in filesystem[2].split(","):
                    continue
                root, mountpoint = Path(fields[3]), Path(fields[4])
                membership = Path(member)
                if not membership.is_relative_to(root):
                    logging.getLogger(__name__).warning("训练cgroup路径不可映射，按1个CPU运行")
                    capacities.append(1)
                    continue
                scope = mountpoint / membership.relative_to(root)
                for directory in (scope, *scope.parents):
                    if not directory.is_relative_to(mountpoint):
                        break
                    if unified:
                        path = directory / "cpu.max"
                        if not path.is_file():
                            continue
                        quota, period = path.read_text(encoding="ascii").split()
                        if quota != "max":
                            capacities.append(_quota_capacity(int(quota), int(period)))
                    else:
                        path = directory / "cpu.cfs_quota_us"
                        if not path.is_file():
                            continue
                        quota_value = int(path.read_text(encoding="ascii"))
                        if quota_value > 0:
                            period_value = int((directory / "cpu.cfs_period_us").read_text(encoding="ascii"))
                            capacities.append(_quota_capacity(quota_value, period_value))
        return tuple(capacities)
    except (OSError, ValueError, IndexError):
        # An unreadable limit must not expand a constrained job to the host CPU count.
        logging.getLogger(__name__).warning("训练CPU额度不可确认，按1个CPU运行")
        return (1,)


def _quota_capacity(quota: int, period: int) -> int:
    if quota <= 0 or period <= 0:
        raise ValueError("training_cpu_quota_invalid")
    return max(1, quota // period)
