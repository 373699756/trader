"""Atomic fitting reservations below the existing process memory scope."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
import sys
from threading import Condition

from trader.training.infra.cgroup import ReadTrainingCgroupScopes


class TrainingMemoryBudget:
    def __init__(self, capacity_bytes: int) -> None:
        if type(capacity_bytes) is not int or capacity_bytes <= 0:
            raise ValueError("training_memory_budget_invalid")
        self.capacity_bytes = capacity_bytes
        self._available = capacity_bytes
        self._condition = Condition()

    @contextmanager
    def reserve(self, required_bytes: int, heartbeat: Callable[[], None]) -> Iterator[None]:
        if type(required_bytes) is not int or not 0 < required_bytes <= self.capacity_bytes:
            raise ValueError(f"training_memory_request_exceeds_budget:{required_bytes}:{self.capacity_bytes}")
        with self._condition:
            while self._available < required_bytes:
                heartbeat()
                self._condition.wait(timeout=0.25)
            heartbeat()
            self._available -= required_bytes
        try:
            yield
        finally:
            with self._condition:
                self._available += required_bytes
                self._condition.notify_all()


def FittingMemoryCapacity() -> int:
    # Reserve 1.5 GiB for Python/native runtimes, streaming windows and page cache.
    capacity = 3 * 1024**3
    try:
        lines = Path("/proc/meminfo").read_text(encoding="ascii").splitlines()
        available = next(int(line.split()[1]) * 1024 for line in lines if line.startswith("MemAvailable:"))
        capacity = min(capacity, max(256 * 1024**2, available - 1536 * 1024**2))
    except (OSError, ValueError, StopIteration):
        pass
    for limit in _cgroup_limits():
        capacity = min(capacity, max(1, limit - 1536 * 1024**2))
    return capacity


def _cgroup_limits() -> tuple[int, ...]:
    if sys.platform != "linux":
        return ()
    try:
        values = []
        for scope in ReadTrainingCgroupScopes("memory"):
            names = ("memory.max", "memory.high") if scope.unified else ("memory.limit_in_bytes",)
            for directory in scope.directories:
                for name in names:
                    path = directory / name
                    if not path.is_file():
                        continue
                    text = path.read_text(encoding="ascii").strip()
                    if (text == "max" and scope.unified) or (text == "-1" and not scope.unified):
                        continue
                    if not text.isdecimal():
                        raise ValueError("training_memory_limit_invalid")
                    values.append(int(text))
        return tuple(values)
    except (OSError, ValueError, IndexError) as exc:
        raise ValueError("training_memory_limit_unavailable") from exc
