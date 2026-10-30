from __future__ import annotations

import os
from pathlib import Path

from trader.download.infra import history_write_tuning
from trader.download.infra.download_settings import LoadHistorySyncConfiguration
from trader.infra import storage_probe
from trader.infra.settings.data_paths import DataPaths

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_storage_profile_reads_parent_disk_for_partition_and_unknown_is_explicit(tmp_path: Path, monkeypatch) -> None:
    device = tmp_path.stat().st_dev
    sysfs = tmp_path / "sysfs"
    partition = tmp_path / "disk/partition"
    partition.mkdir(parents=True)
    rotational = tmp_path / "disk/queue/rotational"
    rotational.parent.mkdir()
    rotational.write_text("1\n")
    (sysfs / f"{os.major(device)}:{os.minor(device)}").parent.mkdir()
    (sysfs / f"{os.major(device)}:{os.minor(device)}").symlink_to(partition)
    monkeypatch.setattr(storage_probe, "_SYSFS_BLOCK_ROOT", sysfs)
    monkeypatch.setattr(storage_probe.sys, "platform", "linux")

    assert storage_probe.StorageProbe().inspect(tmp_path / "new/history").kind == "hdd"
    rotational.write_text("0\n")
    assert storage_probe.StorageProbe().inspect(tmp_path / "new/history").kind == "ssd"
    rotational.write_text("invalid\n")
    assert storage_probe.StorageProbe().inspect(tmp_path / "new/history").kind == "unknown"


def test_only_first_build_on_hdd_uses_larger_write_batch() -> None:
    policy = LoadHistorySyncConfiguration(PROJECT_ROOT, DataPaths(PROJECT_ROOT / "data")).write_batches
    hdd = history_write_tuning.ChooseHistoryWriteTuning("hdd", policy, fresh=True)
    assert (hdd.flush_codes, hdd.flush_cells) == (8, 16384)
    assert (hdd.max_codes, hdd.max_cells) == (64, 32768)
    assert history_write_tuning.ChooseHistoryWriteTuning("hdd", policy, fresh=False).flush_cells == 8192
    assert history_write_tuning.ChooseHistoryWriteTuning("ssd", policy, fresh=True).flush_cells == 8192
    assert history_write_tuning.ChooseHistoryWriteTuning("unknown", policy, fresh=True).flush_cells == 16384
