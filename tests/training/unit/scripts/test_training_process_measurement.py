"""Paging observations distinguish unavailable evidence from measured zero."""

from __future__ import annotations

import sys
import os
from pathlib import Path

import pytest

from scripts.training.resources import training_process_measurement
from scripts.training.resources.training_process_measurement import TrainingProcessMeasurement
from scripts.training.resources.training_process_measurement import (
    ProjectTrainingProcessSnapshot,
    ReadTrainingProcessSnapshot,
)


def _proc(tmp_path: Path, swap: str | None = "0 kB") -> Path:
    root = tmp_path / "self"
    root.mkdir()
    _status(root, swap)
    (root / "io").write_text("read_bytes: 10\nwrite_bytes: 20\n", encoding="utf-8")
    return root


def _status(root: Path, swap: str | None, rss: int = 32, threads: int = 3) -> None:
    contents = f"VmRSS: {rss} kB\nThreads: {threads}\n"
    if swap is not None:
        contents += f"VmSwap: {swap}\n"
    (root / "status").write_text(contents, encoding="utf-8")


def test_missing_swap_preserves_rss_and_thread_sampling_without_inventing_zero(tmp_path: Path) -> None:
    root = _proc(tmp_path, None)
    with TrainingProcessMeasurement(proc_root=root) as measurement:
        _status(root, None, rss=64, threads=5)
    assert measurement.result["observed_peak_rss_bytes"] == 64 * 1024
    assert measurement.result["observed_peak_os_threads"] == 5
    assert measurement.result["sampled_peak_swap_bytes"] is None
    assert measurement.result["system_swap_in_pages"] is None
    assert measurement.result["system_swap_out_pages"] is None
    assert measurement.result["sampling_failures"] == 0


def test_swap_peak_uses_kibibytes_and_retains_an_observed_peak(tmp_path: Path) -> None:
    root = _proc(tmp_path, "16 kB")
    with TrainingProcessMeasurement(proc_root=root) as measurement:
        _status(root, "32 kB")
        measurement._sample()
        _status(root, "8 kB")
    assert measurement.result["sampled_peak_swap_bytes"] == 32 * 1024
    assert measurement.result["sampling_failures"] == 0


@pytest.mark.parametrize("swap", (None, "invalid", "", "-1 kB"))
def test_optional_swap_missing_or_invalid_during_a_run_is_explicitly_unavailable(
    tmp_path: Path, swap: str | None,
) -> None:
    root = _proc(tmp_path)
    with TrainingProcessMeasurement(proc_root=root) as measurement:
        _status(root, swap)
    assert measurement.result["sampled_peak_swap_bytes"] is None
    assert measurement.result["observed_peak_rss_bytes"] == 32 * 1024
    assert measurement.result["sampling_failures"] == 0


def test_page_faults_use_rusage_counter_deltas_and_explicit_process_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    readings = iter(((100, 500), (107, 511)))
    monkeypatch.setattr(training_process_measurement, "_read_fault_counts", lambda: next(readings))
    with TrainingProcessMeasurement(proc_root=_proc(tmp_path)) as measurement:
        pass
    assert measurement.result["major_page_faults"] == 7
    assert measurement.result["minor_page_faults"] == 11
    assert measurement.result["page_fault_scope"] == "current_process; RUSAGE_SELF; excludes_child_processes"


def test_absent_resource_module_returns_null_faults_without_failing_other_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "resource", None)
    with TrainingProcessMeasurement(proc_root=_proc(tmp_path)) as measurement:
        pass
    assert measurement.result["major_page_faults"] is None
    assert measurement.result["minor_page_faults"] is None
    assert measurement.result["sampled_peak_swap_bytes"] == 0
    assert measurement.result["sampling_failures"] == 0


def test_system_swap_reports_pages_and_whole_machine_scope(tmp_path: Path) -> None:
    root = _proc(tmp_path)
    vmstat = tmp_path / "vmstat"
    vmstat.write_text("pswpin 10\npswpout 20\n", encoding="utf-8")
    with TrainingProcessMeasurement(proc_root=root) as measurement:
        vmstat.write_text("pswpin 13\npswpout 24\n", encoding="utf-8")
    assert measurement.result["system_swap_in_pages"] == 3
    assert measurement.result["system_swap_out_pages"] == 4
    assert measurement.result["system_swap_scope"] == "whole_machine; /proc/vmstat; page_count_deltas; includes_other_processes"


@pytest.mark.parametrize("contents", ("", "pswpin invalid\npswpout 20\n", "pswpin -1\npswpout 20\n", "pswpin 9\npswpout 19\n"))
def test_incomplete_invalid_or_reset_system_counters_are_not_reported_as_zero(tmp_path: Path, contents: str) -> None:
    root = _proc(tmp_path)
    vmstat = tmp_path / "vmstat"
    vmstat.write_text("pswpin 10\npswpout 20\n", encoding="utf-8")
    with TrainingProcessMeasurement(proc_root=root) as measurement:
        vmstat.write_text(contents, encoding="utf-8")
    assert measurement.result["system_swap_in_pages"] is None
    assert measurement.result["system_swap_out_pages"] is None
    assert measurement.result["sampling_failures"] == 0


@pytest.mark.skipif(sys.platform != "linux" or not Path("/proc/self/status").is_file(), reason="Linux proc required")
def test_real_linux_proc_and_rusage_supply_actual_paging_evidence() -> None:
    with TrainingProcessMeasurement() as measurement:
        pages = bytearray(1024 * 1024)
        pages[0] = 1
    for field in (
        "sampled_peak_swap_bytes", "major_page_faults", "minor_page_faults", "system_swap_in_pages", "system_swap_out_pages",
    ):
        number = measurement.result[field]
        assert type(number) is int and number >= 0, (field, number)
    assert measurement.result["sampling_failures"] == 0


def _task_stat(pid: int, birth: int = 1234, *, user: int = 70, system: int = 30) -> bytes:
    fields = ["0"] * 22
    fields[0] = "S"
    fields[7], fields[9] = "101", "7"
    fields[11], fields[12], fields[19] = str(user), str(system), str(birth)
    return f"{pid} (worker ) (with spaces)) {' '.join(fields)}\n".encode()


def _external_proc(tmp_path: Path) -> Path:
    if sys.platform != "linux":
        pytest.skip("Linux proc fixture required")
    root = tmp_path / "7412"
    root.mkdir()
    (root / "stat").write_bytes(_task_stat(7412))
    (root / "status").write_text("Pid: 7412\nVmRSS: 64 kB\nVmSwap: 8 kB\nThreads: 2\n", encoding="utf-8")
    (root / "io").write_text(
        "rchar: 1000\nwchar: 2000\nsyscr: 3\nsyscw: 4\nread_bytes: 50\nwrite_bytes: 60\n"
        "cancelled_write_bytes: 7\nprivate_payload: never_project\n", encoding="utf-8",
    )
    for native_id, user, system in ((7412, 40, 10), (7413, 30, 20)):
        task = root / "task" / str(native_id)
        task.mkdir(parents=True)
        (task / "stat").write_bytes(_task_stat(native_id, user=user, system=system))
    return root


def test_external_target_counters_do_not_use_observer_cpu_or_rusage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _external_proc(tmp_path)

    def forbidden() -> float:
        raise AssertionError("observer counters must not be used for a target PID")

    monkeypatch.setattr(training_process_measurement.time, "process_time", forbidden)
    monkeypatch.setattr(training_process_measurement, "_read_fault_counts", forbidden)
    monkeypatch.setattr(training_process_measurement.os, "sysconf", lambda _name: 100)
    snapshot = ReadTrainingProcessSnapshot(7412, proc_root=tmp_path, expected_birth_ticks=1234)
    assert snapshot is not None
    assert (snapshot.pid, snapshot.birth_ticks, snapshot.user_cpu_ticks, snapshot.system_cpu_ticks) == (7412, 1234, 70, 30)
    assert snapshot.comm == "worker ) (with spaces)"
    assert (snapshot.rss_bytes, snapshot.swap_bytes, snapshot.minor_page_faults, snapshot.major_page_faults) == (
        64 * 1024, 8 * 1024, 101, 7,
    )
    assert snapshot.finished_monotonic >= snapshot.started_monotonic
    assert tuple(thread.native_id for thread in snapshot.threads) == (7412, 7413)
    projected = ProjectTrainingProcessSnapshot(snapshot)
    assert projected["cpu_seconds"] == 1.0
    assert projected["io"] == {
        "read_characters": 1000, "written_characters": 2000, "read_calls": 3, "write_calls": 4,
        "physical_read_bytes": 50, "physical_write_bytes": 60, "cancelled_write_bytes": 7,
    }
    assert projected["vanished_thread_ids"] == []
    threads = projected["threads"]
    assert isinstance(threads, list)
    assert threads[0]["cpu_seconds"] == threads[1]["cpu_seconds"] == 0.5
    assert "private_payload" not in str(projected)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux proc required")
def test_missing_or_previously_replaced_target_returns_none(tmp_path: Path) -> None:
    assert ReadTrainingProcessSnapshot(7412, proc_root=tmp_path) is None
    root = _external_proc(tmp_path)
    (root / "io").unlink()
    assert ReadTrainingProcessSnapshot(7412, proc_root=tmp_path, expected_birth_ticks=99) is None


def test_mismatched_stat_pid_is_not_hidden_by_an_expected_birth_mismatch(tmp_path: Path) -> None:
    root = _external_proc(tmp_path)
    (root / "stat").write_bytes(_task_stat(9999))
    with pytest.raises(ValueError, match="pid_mismatch"):
        ReadTrainingProcessSnapshot(7412, proc_root=tmp_path, expected_birth_ticks=99)


def test_external_snapshot_explicitly_rejects_non_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(training_process_measurement.sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="linux_required"):
        ReadTrainingProcessSnapshot(7412, proc_root=tmp_path)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux proc required")
@pytest.mark.parametrize("missing", (False, True))
def test_invalid_proc_root_is_not_reported_as_target_exit(tmp_path: Path, missing: bool) -> None:
    root = tmp_path / "not-proc"
    if not missing:
        root.write_text("not a directory", encoding="utf-8")
    expected = FileNotFoundError if missing else ValueError
    with pytest.raises(expected):
        ReadTrainingProcessSnapshot(7412, proc_root=root)


@pytest.mark.parametrize("missing_io", (False, True))
def test_mismatched_final_stat_pid_precedes_birth_comparison_on_all_recheck_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_io: bool,
) -> None:
    root = _external_proc(tmp_path)
    if missing_io:
        (root / "io").unlink()
    original = training_process_measurement._read_proc_bytes
    reads = 0

    def read(path: Path) -> bytes:
        nonlocal reads
        if path == root / "stat":
            reads += 1
            if reads == 2:
                return _task_stat(9999, 9999)
        return original(path)

    monkeypatch.setattr(training_process_measurement, "_read_proc_bytes", read)
    with pytest.raises(ValueError, match="pid_mismatch"):
        ReadTrainingProcessSnapshot(7412, proc_root=tmp_path)


@pytest.mark.parametrize("replaced", (False, True))
def test_target_exit_or_pid_reuse_during_the_read_discards_all_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replaced: bool,
) -> None:
    root = _external_proc(tmp_path)
    original = training_process_measurement._read_proc_bytes
    reads = 0

    def read(path: Path) -> bytes:
        nonlocal reads
        if path == root / "stat":
            reads += 1
            if reads == 2:
                if replaced:
                    return _task_stat(7412, 9999)
                raise ProcessLookupError("target exited")
        return original(path)

    monkeypatch.setattr(training_process_measurement, "_read_proc_bytes", read)
    assert ReadTrainingProcessSnapshot(7412, proc_root=tmp_path) is None


def test_thread_exit_is_explicit_and_does_not_discard_the_surviving_process(tmp_path: Path) -> None:
    root = _external_proc(tmp_path)
    (root / "task/7413/stat").unlink()
    snapshot = ReadTrainingProcessSnapshot(7412, proc_root=tmp_path)
    assert snapshot is not None
    assert snapshot.vanished_thread_ids == (7413,)
    assert snapshot.reported_threads == 2
    assert tuple(thread.native_id for thread in snapshot.threads) == (7412,)


def test_missing_file_on_surviving_target_is_not_reported_as_process_exit(tmp_path: Path) -> None:
    root = _external_proc(tmp_path)
    (root / "io").unlink()
    with pytest.raises(FileNotFoundError):
        ReadTrainingProcessSnapshot(7412, proc_root=tmp_path)


def test_target_permission_error_is_not_hidden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _external_proc(tmp_path)
    original = training_process_measurement._read_proc_bytes

    def read(path: Path) -> bytes:
        if path == root / "io":
            raise PermissionError("denied")
        return original(path)

    monkeypatch.setattr(training_process_measurement, "_read_proc_bytes", read)
    with pytest.raises(PermissionError):
        ReadTrainingProcessSnapshot(7412, proc_root=tmp_path)


@pytest.mark.parametrize("name,contents", (
    ("stat", b"7412 (broken) S 1 2\n"),
    ("io", b"read_bytes: broken\n"),
    ("status", b"Pid: 7412\nThreads: 2\nVmRSS: -1 kB\n"),
    ("status", b"Pid: 7412\nThreads: 2\nVmRSS: 1 MB\n"),
    ("status", b"x" * 65537),
))
def test_malformed_target_files_fail_instead_of_inventing_zero(
    tmp_path: Path, name: str, contents: bytes,
) -> None:
    root = _external_proc(tmp_path)
    (root / name).write_bytes(contents)
    with pytest.raises(ValueError, match="training_process_snapshot_"):
        ReadTrainingProcessSnapshot(7412, proc_root=tmp_path)


def test_missing_optional_memory_is_unavailable_not_zero(tmp_path: Path) -> None:
    root = _external_proc(tmp_path)
    (root / "status").write_text("Pid: 7412\nThreads: 2\n", encoding="utf-8")
    snapshot = ReadTrainingProcessSnapshot(7412, proc_root=tmp_path)
    assert snapshot is not None
    assert snapshot.rss_bytes is snapshot.swap_bytes is None


@pytest.mark.parametrize("pid", (True, 0, -1))
def test_invalid_target_pid_is_rejected(pid: int) -> None:
    with pytest.raises(ValueError, match="pid_invalid"):
        ReadTrainingProcessSnapshot(pid)


@pytest.mark.skipif(sys.platform != "linux" or not Path("/proc/self/stat").is_file(), reason="Linux proc required")
def test_real_linux_external_snapshot_uses_proc_birth_and_thread_identities() -> None:
    snapshot = ReadTrainingProcessSnapshot(os.getpid())
    assert snapshot is not None and snapshot.pid == os.getpid()
    assert snapshot.birth_ticks > 0 and snapshot.clock_ticks_per_second > 0
    assert any(thread.native_id == snapshot.pid for thread in snapshot.threads)
    assert ReadTrainingProcessSnapshot(snapshot.pid, expected_birth_ticks=snapshot.birth_ticks) is not None
