from __future__ import annotations

import os
from pathlib import Path

from trader.download.infra import history_storage_profile


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
    monkeypatch.setattr(history_storage_profile, "_SYSFS_BLOCK_ROOT", sysfs)

    assert history_storage_profile.ClassifyHistoryStorage(tmp_path / "new/history") == "hdd"
    rotational.write_text("0\n")
    assert history_storage_profile.ClassifyHistoryStorage(tmp_path / "new/history") == "ssd"
    rotational.write_text("invalid\n")
    assert history_storage_profile.ClassifyHistoryStorage(tmp_path / "new/history") == "unknown"


def test_only_first_build_on_hdd_uses_larger_write_batch() -> None:
    hdd = history_storage_profile.ChooseHistoryWriteTuning("hdd", fresh=True)
    assert (hdd.flush_codes, hdd.flush_cells) == (8, 16384)
    assert (hdd.max_codes, hdd.max_cells) == (64, 32768)
    assert history_storage_profile.ChooseHistoryWriteTuning("hdd", fresh=False).flush_cells == 8192
    assert history_storage_profile.ChooseHistoryWriteTuning("ssd", fresh=True).flush_cells == 8192
    assert history_storage_profile.ChooseHistoryWriteTuning("unknown", fresh=True).flush_cells == 8192
