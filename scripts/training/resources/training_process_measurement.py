"""Observe each diagnostic training call without changing product resource limits."""

from __future__ import annotations

import time
import shutil
from pathlib import Path
from threading import Event, Thread
from types import TracebackType


class TrainingProcessMeasurement:
    """Sample Linux RSS and OS threads; peaks are observed lower bounds, not hard limits."""

    def __init__(self, *, proc_root: Path = Path("/proc/self"), disk_root: Path | None = None) -> None:
        self._root = proc_root
        self._stop = Event()
        self._worker = Thread(target=self._observe, name="training-diagnostic-resources", daemon=False)
        self._rss: int | None = None
        self._threads: int | None = None
        self._samples = 0
        self._failures = 0
        self._cpu = 0.0
        self._io: tuple[int, int] | None = None
        self._disk_root = disk_root
        self._start_free: int | None = None
        self._minimum_free: int | None = None
        self.result: dict[str, object] = {}

    def __enter__(self) -> TrainingProcessMeasurement:
        self._cpu = time.process_time()
        self._io = self._read_io()
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

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self._stop.set()
        self._worker.join()
        self._sample()
        after = self._read_io()
        self.result = {
            "cpu_seconds": time.process_time() - self._cpu,
            "observed_peak_rss_bytes": self._rss,
            "observed_peak_os_threads": self._threads,
            "physical_read_bytes": max(0, after[0] - self._io[0]) if after and self._io else None,
            "physical_write_bytes": max(0, after[1] - self._io[1]) if after and self._io else None,
            "samples": self._samples,
            "sampling_failures": self._failures,
            "sampling_interval_seconds": 0.25,
            "scope": "current_process; OS_threads_include_one_observer; peaks_are_sampled_lower_bounds",
            "native_thread_utilization_verified": False,
            "filesystem_free_bytes_at_start": self._start_free,
            "filesystem_minimum_free_bytes": self._minimum_free,
            "filesystem_measurement_scope": "whole_filesystem_including_other_processes; sampled_minimum",
        }


__all__ = ["TrainingProcessMeasurement"]
