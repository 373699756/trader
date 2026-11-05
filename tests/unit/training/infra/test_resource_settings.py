"""Independent machine policies reach actual training admission and stream limits."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from datetime import date
from pathlib import Path
from queue import Queue

import pytest

from tests.helpers.training_resources import TrainingResourceSettingsFixture
from trader.download.domain.history_revision import HistoryTrainingRow
from trader.infra.settings import LoadRuntimeSettings
from trader.infra.settings.training import LoadTrainingResourceSettings
from trader.training.application.resources import TrainingFanoutSettings
from trader.training.application.selected_training import RunSelectedTrainingTasks, SelectedTrainingTask
from trader.training.infra.cpu_admission import TrainingCpuBudget
from trader.training.infra.memory_admission import FittingMemoryCapacity

ROOT = Path(__file__).resolve().parents[4]


def _configuration(tmp_path):
    document = json.loads((ROOT / "config/training.json").read_text())
    (tmp_path / "config").mkdir()
    return document


def _write(tmp_path, document):
    (tmp_path / "config/training.json").write_text(json.dumps(document))


@pytest.mark.parametrize("medium", ("hdd", "ssd", "unknown"))
def test_each_machine_has_its_own_policy_and_preserves_default_behavior(tmp_path, medium):
    document = _configuration(tmp_path)
    document["resources"]["ssd"]["cpu"]["maximum_threads"] = 5
    document["resources"]["ssd"]["fanout"] = {"batch_rows": 17, "queue_batches": 3}
    _write(tmp_path, document)
    settings = LoadTrainingResourceSettings(tmp_path, medium)
    assert settings.cpu.maximum_threads == (5 if medium == "ssd" else None)
    assert settings.fanout.batch_rows == (17 if medium == "ssd" else 256)
    assert TrainingResourceSettingsFixture().memory.maximum_bytes == 3 * 1024**3


@pytest.mark.parametrize("section,key,value", (
    ("cpu", "maximum_threads", True), ("cpu", "maximum_threads", 0),
    ("cpu", "coordinator_cpus", -1), ("cpu", "native_threads", 1.5),
    ("memory", "maximum_bytes", 0), ("memory", "reserve_bytes", -1),
    ("memory", "minimum_available_bytes", 4 * 1024**3),
    ("fanout", "batch_rows", 0), ("fanout", "queue_batches", True),
    ("fanout", "queue_batches", float("inf")),
    ("samples", "sqlite_cache_bytes", 1025), ("samples", "v1_sqlite_cache_bytes", 0),
    ("samples", "sqlite_mmap_bytes", -1), ("samples", "matrix_memory_bytes", True),
    ("samples", "sequence_cache_bytes", 1.5), ("samples", "sequence_writer_codes", 0),
    ("samples", "v1_batch_rows", True), ("samples", "tcn_batch_memory_bytes", -1),
))
def test_invalid_values_in_either_machine_are_rejected(tmp_path, section, key, value):
    document = _configuration(tmp_path)
    document["resources"]["ssd"][section][key] = value
    _write(tmp_path, document)
    with pytest.raises(ValueError):
        LoadTrainingResourceSettings(tmp_path, "hdd")


@pytest.mark.parametrize("mutation", (
    "missing_resources", "missing_machine", "unknown_field", "missing_samples", "missing_sample_field", "unknown_sample_field",
))
def test_configuration_has_one_required_strict_resource_schema(tmp_path, mutation):
    document = _configuration(tmp_path)
    if mutation == "missing_resources":
        del document["resources"]
    elif mutation == "missing_machine":
        del document["resources"]["ssd"]
    elif mutation == "missing_samples":
        del document["resources"]["ssd"]["samples"]
    elif mutation == "missing_sample_field":
        del document["resources"]["ssd"]["samples"]["sqlite_cache_bytes"]
    elif mutation == "unknown_sample_field":
        document["resources"]["ssd"]["samples"]["sqlite_cache_mib"] = 32
    else:
        document["resources"]["hdd"]["cpu"]["threads"] = 4
    _write(tmp_path, document)
    with pytest.raises(ValueError):
        LoadTrainingResourceSettings(tmp_path, "hdd")


def test_only_explicit_archived_diagnostic_configuration_accepts_old_root_schema(tmp_path):
    from scripts.checks.training.v5_lite import _parameters

    document = _configuration(tmp_path)
    _write(tmp_path, document)
    path = tmp_path / "config/training.json"
    current = _parameters(path)
    del document["resources"]
    _write(tmp_path, document)
    with pytest.raises(ValueError):
        _parameters(path)
    assert _parameters(path, archived=True) == current


@pytest.mark.parametrize("medium,expected", (("hdd", (2, 1, 7, 1)), ("ssd", (5, 3, 19, 3))))
def test_nondefault_machine_settings_reach_training_composition_root(tmp_path, monkeypatch, medium, expected):
    from trader.bootstrap import ExecuteSelectedTraining
    from trader.training.infra import cpu_admission, memory_admission

    document = _configuration(tmp_path)
    for name, maximum, native, batch, queues in (("hdd", 2, 1, 7, 1), ("ssd", 5, 3, 19, 3)):
        resource = document["resources"][name]
        resource["cpu"].update(maximum_threads=maximum, native_threads=native, coordinator_cpus=2)
        resource["memory"].update(maximum_bytes=1000, reserve_bytes=100, minimum_available_bytes=50)
        resource["fanout"].update(batch_rows=batch, queue_batches=queues)
        resource["samples"].update(sqlite_cache_bytes=8192, v1_batch_rows=batch, matrix_memory_bytes=0)
    _write(tmp_path, document)
    runtime = replace(LoadRuntimeSettings(ROOT / "config/runtime.json"), project_root=tmp_path, disk_medium=medium)
    observed = []
    monkeypatch.setattr(cpu_admission, "AvailableTrainingCpus", lambda: 12)
    monkeypatch.setattr("trader.training.entrypoints.commands.ConfigureTrainingResources", observed.append)
    monkeypatch.setattr(memory_admission, "FittingMemoryCapacity", lambda setting: setting.maximum_bytes - setting.reserve_bytes)

    def run(_runtime, profiles, **dependencies):
        assert profiles == ("v3", "v5")
        cpu = dependencies["cpu_budget"]
        fanout = dependencies["fanout_settings"]
        assert (cpu.capacity, cpu.native_threads, fanout.batch_rows, fanout.queue_batches) == expected
        assert dependencies["memory_admission"].capacity_bytes == 900
        samples = dependencies["sample_settings"]
        assert (samples.sqlite_cache_bytes, samples.v1_batch_rows, samples.matrix_memory_bytes) == (8192, expected[2], 0)
        assert dependencies["executor"].status().running is False
        return 0

    monkeypatch.setattr("trader.training.entrypoints.selected_training.RunSelectedTraining", run)
    assert ExecuteSelectedTraining(runtime, ("v3", "v5")) == 0
    assert observed == [expected[1]]


def test_cpu_and_memory_configuration_remain_below_actual_machine_limits(monkeypatch):
    from trader.training.infra import memory_admission

    loaded = TrainingResourceSettingsFixture()
    cpu = replace(loaded.cpu, coordinator_cpus=2, maximum_threads=7, native_threads=3)
    assert (TrainingCpuBudget(16, cpu).capacity, TrainingCpuBudget(4, cpu).capacity) == (7, 2)
    assert TrainingCpuBudget(1, cpu).native_threads == 1
    memory = replace(loaded.memory, maximum_bytes=10_000, reserve_bytes=1024, minimum_available_bytes=256)
    monkeypatch.setattr(memory_admission, "_cgroup_limits", lambda: (2500, 2000))
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: "MemAvailable: 8 kB\n")
    assert FittingMemoryCapacity(memory) == 976
    monkeypatch.setattr(memory_admission, "_cgroup_limits", lambda: ())
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: "MemAvailable: 1 kB\n")
    assert FittingMemoryCapacity(memory) == 256


def test_nondefault_batch_and_queue_limits_preserve_all_rows_and_order(monkeypatch):
    from trader.training.application import selected_training

    queue_limits, batches = [], []
    original = selected_training._TrainingFanout._dispatch

    def queue(*, maxsize):
        queue_limits.append(maxsize)
        return Queue(maxsize=maxsize)

    def dispatch(self, rows):
        batches.append(len(rows))
        return original(self, rows)

    monkeypatch.setattr(selected_training, "Queue", queue)
    monkeypatch.setattr(selected_training._TrainingFanout, "_dispatch", dispatch)
    day = date(2026, 1, 5)
    rows = tuple(HistoryTrainingRow(f"{number:06d}", day, "main", "bank", None) for number in range(11))
    with ThreadPoolExecutor(max_workers=2) as executor:
        run = RunSelectedTrainingTasks(
            tuple(SelectedTrainingTask(profile, frozenset((day,)), tuple) for profile in ("v3", "v5")),
            lambda _dates: nullcontext(iter(rows)),
            executor,
            lambda: False,
            settings=TrainingFanoutSettings(batch_rows=3, queue_batches=1),
        )
    assert queue_limits == [1, 1]
    assert batches == [3, 3, 3, 2]
    assert run.history_scan_passes == 1
    assert all(outcome.result == rows for outcome in run.outcomes)
