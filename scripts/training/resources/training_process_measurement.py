"""Observe each diagnostic training call without changing product resource limits."""

from __future__ import annotations

import time
import shutil
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISDIR
from threading import Event, Thread
from types import TracebackType


@dataclass(frozen=True)
class TrainingThreadSnapshot:
    native_id: int
    birth_ticks: int
    state: str
    comm: str
    user_cpu_ticks: int
    system_cpu_ticks: int
    minor_page_faults: int
    major_page_faults: int


@dataclass(frozen=True)
class TrainingProcessIoSnapshot:
    read_characters: int
    written_characters: int
    read_calls: int
    write_calls: int
    physical_read_bytes: int
    physical_write_bytes: int
    cancelled_write_bytes: int


@dataclass(frozen=True)
class TrainingProcessSnapshot:
    pid: int
    birth_ticks: int
    state: str
    comm: str
    user_cpu_ticks: int
    system_cpu_ticks: int
    minor_page_faults: int
    major_page_faults: int
    rss_bytes: int | None
    swap_bytes: int | None
    reported_threads: int
    io: TrainingProcessIoSnapshot
    threads: tuple[TrainingThreadSnapshot, ...]
    vanished_thread_ids: tuple[int, ...]
    clock_ticks_per_second: int
    started_monotonic: float
    finished_monotonic: float


def ReadTrainingProcessSnapshot(
    pid: int,
    *,
    proc_root: Path = Path("/proc"),
    expected_birth_ticks: int | None = None,
) -> TrainingProcessSnapshot | None:
    """Read Linux target counters, returning None only when that identity is gone.

    The proc root must exist as a directory; unsupported platforms fail explicitly.
    The process birth identity is checked before and after the bounded read window.
    Threads can exit independently and are explicitly listed as vanished; the
    surviving thread list and process counters are not an atomic kernel snapshot.
    Permissions, malformed data and missing files on a surviving target propagate.
    No CPU or fault counters from the observing process are used.
    """
    if type(pid) is not int or pid <= 0:
        raise ValueError("training_process_snapshot_pid_invalid")
    if expected_birth_ticks is not None and (type(expected_birth_ticks) is not int or expected_birth_ticks < 0):
        raise ValueError("training_process_snapshot_birth_invalid")
    if sys.platform != "linux":
        raise RuntimeError("training_process_snapshot_linux_required")
    if not S_ISDIR(proc_root.stat().st_mode):
        raise ValueError("training_process_snapshot_proc_root_invalid")
    started = time.monotonic()
    root = proc_root / str(pid)
    before = _read_target_stat(root)
    if before is None:
        return None
    if before.native_id != pid:
        raise ValueError("training_process_snapshot_pid_mismatch")
    if expected_birth_ticks is not None and before.birth_ticks != expected_birth_ticks:
        return None
    clock_ticks = os.sysconf("SC_CLK_TCK")
    if type(clock_ticks) is not int or clock_ticks <= 0:
        raise ValueError("training_process_snapshot_clock_invalid")
    try:
        status = _read_proc_fields(root / "status")
        if _proc_counter(status, "Pid") != pid:
            raise ValueError("training_process_snapshot_status_pid_mismatch")
        rss, swap = _proc_memory(status, "VmRSS"), _proc_memory(status, "VmSwap")
        reported_threads = _proc_counter(status, "Threads")
        io = _read_target_io(root / "io")
        threads, vanished = _read_target_threads(root / "task")
    except (FileNotFoundError, ProcessLookupError) as exc:
        after = _read_target_stat(root)
        if after is None:
            return None
        if after.native_id != pid:
            raise ValueError("training_process_snapshot_pid_mismatch") from exc
        if after.birth_ticks != before.birth_ticks:
            return None
        raise
    after = _read_target_stat(root)
    if after is None:
        return None
    if after.native_id != pid:
        raise ValueError("training_process_snapshot_pid_mismatch")
    if after.birth_ticks != before.birth_ticks:
        return None
    return TrainingProcessSnapshot(
        pid=pid,
        birth_ticks=after.birth_ticks,
        state=after.state,
        comm=after.comm,
        user_cpu_ticks=after.user_cpu_ticks,
        system_cpu_ticks=after.system_cpu_ticks,
        minor_page_faults=after.minor_page_faults,
        major_page_faults=after.major_page_faults,
        rss_bytes=rss,
        swap_bytes=swap,
        reported_threads=reported_threads,
        io=io,
        threads=threads,
        vanished_thread_ids=vanished,
        clock_ticks_per_second=clock_ticks,
        started_monotonic=started,
        finished_monotonic=time.monotonic(),
    )


def ProjectTrainingProcessSnapshot(snapshot: TrainingProcessSnapshot) -> dict[str, object]:
    """Project the external diagnostic snapshot through an explicit field whitelist."""
    ticks = snapshot.clock_ticks_per_second
    return {
        "schema_version": "training-external-process-snapshot",
        "pid": snapshot.pid,
        "birth_ticks": snapshot.birth_ticks,
        "state": snapshot.state,
        "comm": snapshot.comm,
        "clock_ticks_per_second": ticks,
        "user_cpu_ticks": snapshot.user_cpu_ticks,
        "system_cpu_ticks": snapshot.system_cpu_ticks,
        "cpu_seconds": (snapshot.user_cpu_ticks + snapshot.system_cpu_ticks) / ticks,
        "minor_page_faults": snapshot.minor_page_faults,
        "major_page_faults": snapshot.major_page_faults,
        "rss_bytes": snapshot.rss_bytes,
        "swap_bytes": snapshot.swap_bytes,
        "reported_threads": snapshot.reported_threads,
        "io": {
            "read_characters": snapshot.io.read_characters,
            "written_characters": snapshot.io.written_characters,
            "read_calls": snapshot.io.read_calls,
            "write_calls": snapshot.io.write_calls,
            "physical_read_bytes": snapshot.io.physical_read_bytes,
            "physical_write_bytes": snapshot.io.physical_write_bytes,
            "cancelled_write_bytes": snapshot.io.cancelled_write_bytes,
        },
        "threads": [_project_target_thread(thread, ticks) for thread in snapshot.threads],
        "vanished_thread_ids": list(snapshot.vanished_thread_ids),
        "started_monotonic": snapshot.started_monotonic,
        "finished_monotonic": snapshot.finished_monotonic,
        "scope": "target_process_lifetime; excludes_children; non_atomic_read_window; no_observer_counters",
        "thread_scope": "observed_target_threads; vanished_threads_explicit; not_all_time_thread_history",
    }


def _project_target_thread(thread: TrainingThreadSnapshot, clock_ticks: int) -> dict[str, object]:
    return {
        "native_id": thread.native_id,
        "birth_ticks": thread.birth_ticks,
        "state": thread.state,
        "comm": thread.comm,
        "user_cpu_ticks": thread.user_cpu_ticks,
        "system_cpu_ticks": thread.system_cpu_ticks,
        "cpu_seconds": (thread.user_cpu_ticks + thread.system_cpu_ticks) / clock_ticks,
        "minor_page_faults": thread.minor_page_faults,
        "major_page_faults": thread.major_page_faults,
    }


def _read_proc_bytes(path: Path) -> bytes:
    with path.open("rb") as stream:
        contents = stream.read(65537)
    if len(contents) > 65536:
        raise ValueError("training_process_snapshot_file_too_large")
    return contents


def _read_target_stat(root: Path) -> TrainingThreadSnapshot | None:
    try:
        return _parse_target_stat(_read_proc_bytes(root / "stat"))
    except (FileNotFoundError, ProcessLookupError):
        return None


def _parse_target_stat(contents: bytes) -> TrainingThreadSnapshot:
    try:
        prefix, separator, tail = contents.rpartition(b")")
        pid_text, opening, comm = prefix.partition(b"(")
        fields = tail.split()
        if not separator or not opening or len(fields) < 20 or len(fields[0]) != 1:
            raise ValueError("stat_shape_invalid")
        values = tuple(int(value) for value in (pid_text, fields[7], fields[9], fields[11], fields[12], fields[19]))
        if values[0] <= 0 or any(value < 0 for value in values[1:]):
            raise ValueError("stat_counter_invalid")
        state = fields[0].decode("ascii")
    except (IndexError, UnicodeError, ValueError) as exc:
        raise ValueError("training_process_snapshot_stat_invalid") from exc
    return TrainingThreadSnapshot(
        native_id=values[0],
        birth_ticks=values[5],
        state=state,
        comm=comm.decode("utf-8", errors="replace"),
        user_cpu_ticks=values[3],
        system_cpu_ticks=values[4],
        minor_page_faults=values[1],
        major_page_faults=values[2],
    )


def _read_proc_fields(path: Path) -> dict[str, str]:
    return {
        key: value.strip()
        for line in _read_proc_bytes(path).decode("ascii", errors="replace").splitlines() if ":" in line
        for key, value in (line.split(":", 1),)
    }


def _proc_counter(fields: dict[str, str], name: str) -> int:
    try:
        value = int(fields[name])
        if value < 0:
            raise ValueError("counter_negative")
        return value
    except (KeyError, ValueError) as exc:
        raise ValueError("training_process_snapshot_counter_invalid:" + name) from exc


def _proc_memory(fields: dict[str, str], name: str) -> int | None:
    if name not in fields:
        return None
    parts = fields[name].split()
    if len(parts) != 2 or parts[1] != "kB":
        raise ValueError("training_process_snapshot_memory_invalid:" + name)
    return _proc_counter({name: parts[0]}, name) * 1024


def _read_target_io(path: Path) -> TrainingProcessIoSnapshot:
    fields = _read_proc_fields(path)
    return TrainingProcessIoSnapshot(
        _proc_counter(fields, "rchar"),
        _proc_counter(fields, "wchar"),
        _proc_counter(fields, "syscr"),
        _proc_counter(fields, "syscw"),
        _proc_counter(fields, "read_bytes"),
        _proc_counter(fields, "write_bytes"),
        _proc_counter(fields, "cancelled_write_bytes"),
    )


def _read_target_threads(root: Path) -> tuple[tuple[TrainingThreadSnapshot, ...], tuple[int, ...]]:
    identifiers = []
    for path in root.iterdir():
        if path.name.isascii() and path.name.isdecimal():
            identifiers.append(int(path.name))
            if len(identifiers) > 65536:
                raise ValueError("training_process_snapshot_thread_limit")
    threads = []
    vanished = []
    for native_id in sorted(identifiers):
        value = _read_target_stat(root / str(native_id))
        if value is None:
            vanished.append(native_id)
        elif value.native_id != native_id:
            raise ValueError("training_process_snapshot_thread_pid_mismatch")
        else:
            threads.append(value)
    return tuple(threads), tuple(vanished)


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


__all__ = [
    "TrainingProcessMeasurement",
    "TrainingProcessSnapshot",
    "TrainingProcessIoSnapshot",
    "TrainingThreadSnapshot",
    "ReadTrainingProcessSnapshot",
    "ProjectTrainingProcessSnapshot",
]
