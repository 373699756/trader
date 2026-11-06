"""Paging observations distinguish unavailable evidence from measured zero."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from scripts.training.resources import training_process_measurement
from scripts.training.resources.training_process_measurement import TrainingProcessMeasurement


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
