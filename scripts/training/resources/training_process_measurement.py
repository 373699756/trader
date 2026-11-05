"""Observe each diagnostic training call without changing product resource limits."""

from __future__ import annotations

import time
import shutil
from pathlib import Path
from threading import Event, Thread
from types import TracebackType


class TrainingProcessMeasurement:
    """Observe process peaks and paging; sampled peaks are lower bounds, not hard limits."""

    def __init__(self, *, proc_root: Path = Path("/proc/self"), disk_root: Path | None = None) -> None:
        self._root = proc_root
        self._stop = Event()
        self._worker = Thread(target=self._observe, name="training-diagnostic-resources", daemon=False)
        self._rss: int | None = None
        self._swap: int | None = None
        self._swap_complete = True
        self._threads: int | None = None
        self._samples = 0
        self._failures = 0
        self._cpu = 0.0
        self._io: tuple[int, int] | None = None
        self._faults: tuple[int, int] | None = None
        self._system_swap: tuple[int, int] | None = None
        self._disk_root = disk_root
        self._start_free: int | None = None
        self._minimum_free: int | None = None
        self.result: dict[str, object] = {}

    def __enter__(self) -> TrainingProcessMeasurement:
        self._cpu = time.process_time()
        self._io = self._read_io()
        self._faults = _read_fault_counts()
        self._system_swap = self._read_system_swap()
        self._sample()
        self._worker.start()
        return self

    def _sample(self) -> None:
        if self._disk_root is not None:
            try:
                free = shutil.disk_usage(self._disk_root).free
                if self._start_free is None:
                    self._start_free = free
                self._minimum_free = min(self._minimum_free, free) if self._minimum_free is not None else free
            except OSError:
                self._failures += 1
        try:
            with (self._root / "status").open(encoding="utf-8") as stream:
                fields = dict(line.split(":", 1) for line in stream.read(65536).splitlines() if ":" in line)
            rss = int(fields["VmRSS"].split()[0]) * 1024
            threads = int(fields["Threads"].strip())
        except (OSError, ValueError, KeyError):
            self._failures += 1
            return
        self._rss = max(self._rss or 0, rss)
        self._threads = max(self._threads or 0, threads)
        try:
            swap = int(fields["VmSwap"].split()[0]) * 1024
            if swap < 0:
                raise ValueError("negative_swap")
        except (ValueError, KeyError, IndexError):
            self._swap_complete = False
        else:
            self._swap = max(self._swap or 0, swap)
        self._samples += 1

    def _observe(self) -> None:
        while not self._stop.wait(0.25):
            self._sample()

    def _read_io(self) -> tuple[int, int] | None:
        try:
            with (self._root / "io").open(encoding="utf-8") as stream:
                fields = dict(line.split(":", 1) for line in stream.read(65536).splitlines() if ":" in line)
            return int(fields["read_bytes"]), int(fields["write_bytes"])
        except (OSError, ValueError, KeyError):
            return None

    def _read_system_swap(self) -> tuple[int, int] | None:
        try:
            with (self._root.parent / "vmstat").open(encoding="utf-8") as stream:
                fields = dict(line.split() for line in stream.read(65536).splitlines() if len(line.split()) == 2)
            counts = int(fields["pswpin"]), int(fields["pswpout"])
            return counts if all(count >= 0 for count in counts) else None
        except (OSError, ValueError, KeyError):
            return None

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self._stop.set()
        self._worker.join()
        self._sample()
        after = self._read_io()
        faults = _read_fault_counts()
        system_swap = self._read_system_swap()
        self.result = {
            "cpu_seconds": time.process_time() - self._cpu,
            "observed_peak_rss_bytes": self._rss,
            "observed_peak_os_threads": self._threads,
            "physical_read_bytes": max(0, after[0] - self._io[0]) if after and self._io else None,
            "physical_write_bytes": max(0, after[1] - self._io[1]) if after and self._io else None,
            "sampled_peak_swap_bytes": self._swap if self._swap_complete else None,
            "major_page_faults": _counter_delta(self._faults, faults, 0),
            "minor_page_faults": _counter_delta(self._faults, faults, 1),
            "page_fault_scope": "current_process; RUSAGE_SELF; excludes_child_processes",
            "system_swap_in_pages": _counter_delta(self._system_swap, system_swap, 0),
            "system_swap_out_pages": _counter_delta(self._system_swap, system_swap, 1),
            "system_swap_scope": "whole_machine; /proc/vmstat; page_count_deltas; includes_other_processes",
            "samples": self._samples,
            "sampling_failures": self._failures,
            "sampling_interval_seconds": 0.25,
            "scope": "current_process; OS_threads_include_one_observer; peaks_are_sampled_lower_bounds",
            "native_thread_utilization_verified": False,
            "filesystem_free_bytes_at_start": self._start_free,
            "filesystem_minimum_free_bytes": self._minimum_free,
            "filesystem_measurement_scope": "whole_filesystem_including_other_processes; sampled_minimum",
        }


def _read_fault_counts() -> tuple[int, int] | None:
    try:
        import resource
    except ImportError:
        return None
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
    except (OSError, ValueError):
        return None
    return usage.ru_majflt, usage.ru_minflt


def _counter_delta(before: tuple[int, int] | None, after: tuple[int, int] | None, index: int) -> int | None:
    if before is None or after is None or after[index] < before[index]:
        return None
    return after[index] - before[index]


__all__ = ["TrainingProcessMeasurement"]
