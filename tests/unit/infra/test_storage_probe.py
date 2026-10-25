from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from trader.infra import storage_platform, storage_probe
from trader.infra.settings import LoadRuntimeSettings
from trader.infra.settings.parser import ConfigurationError
from trader.infra.settings.storage import ParseStorageOverrides
from trader.infra.storage_runtime import InspectDataStorage, ReportStorageProfiles
from trader.infra.storage_types import StorageOverride

ROOT = Path(__file__).resolve().parents[3]


def _disk(path: Path, rotational: str) -> Path:
    (path / "queue").mkdir(parents=True)
    (path / "queue/rotational").write_text(rotational)
    return path


def _sysfs(tmp_path: Path, node: Path, monkeypatch) -> None:
    device = tmp_path.stat().st_dev
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    (sysfs / f"{os.major(device)}:{os.minor(device)}").symlink_to(node)
    monkeypatch.setattr(storage_probe, "_SYSFS_BLOCK_ROOT", sysfs)
    monkeypatch.setattr(storage_probe.sys, "platform", "linux")


def test_cache_is_per_invocation_and_device(tmp_path, monkeypatch):
    disk = _disk(tmp_path / "disk", "1")
    _sysfs(tmp_path, disk, monkeypatch)
    probe = storage_probe.StorageProbe()
    first = probe.inspect(tmp_path / "not-created/history")
    (disk / "queue/rotational").write_text("0")
    assert probe.inspect(tmp_path / "other/samples") is first
    assert first.kind == "hdd"
    assert storage_probe.StorageProbe().inspect(tmp_path).kind == "ssd"
    assert not (tmp_path / "not-created").exists()


def test_linux_mixed_devices_and_virtual_queue(tmp_path, monkeypatch):
    volume = _disk(tmp_path / "virtual/md0", "0")
    hdd = _disk(tmp_path / "sda", "1")
    ssd = _disk(tmp_path / "sdb", "0")
    (volume / "slaves").mkdir()
    (volume / "slaves/a").symlink_to(hdd)
    (volume / "slaves/b").symlink_to(ssd)
    _sysfs(tmp_path, volume, monkeypatch)
    assert storage_probe.StorageProbe().inspect(tmp_path).kind == "hdd"
    (hdd / "queue/rotational").write_text("bad")
    assert storage_probe.StorageProbe().inspect(tmp_path).kind == "unknown"
    (volume / "slaves/a").unlink()
    (volume / "slaves/b").unlink()
    assert storage_probe.StorageProbe().inspect(tmp_path).kind == "unknown"


def test_manual_override_longest_scope_and_resolved_symlink(tmp_path):
    target = tmp_path / "mounted"
    target.mkdir()
    (tmp_path / "samples").symlink_to(target, target_is_directory=True)
    overrides = ParseStorageOverrides(
        {"overrides": [{"path": ".", "kind": "hdd"}, {"path": "samples", "kind": "ssd"}]}, tmp_path
    )
    probe = storage_probe.StorageProbe(overrides)
    result = probe.inspect(tmp_path / "samples/new")
    assert result.kind == "ssd" and result.source == "configuration"
    assert probe.inspect(tmp_path / "history").kind == "hdd"


@pytest.mark.parametrize(
    "overrides",
    [
        [{"path": ".", "kind": "unknown"}],
        [{"path": ".", "kind": "SSD"}],
        [{"path": ".", "kind": "hdd", "other": True}],
        [{"path": ".", "kind": "hdd"}, {"path": "./", "kind": "ssd"}],
        [None],
        {},
    ],
)
def test_invalid_overrides_are_errors(tmp_path, overrides):
    with pytest.raises(ConfigurationError):
        ParseStorageOverrides({"overrides": overrides}, tmp_path)


def test_runtime_config_storage_is_optional_and_uses_actual_config(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config"
    shutil.copytree(ROOT / "config", config)
    path = config / "runtime.json"
    raw = json.loads(path.read_text())
    raw.pop("storage")
    path.write_text(json.dumps(raw))
    settings = LoadRuntimeSettings(path)
    assert settings.storage_overrides == ()
    monkeypatch.setattr(storage_probe, "_SYSFS_BLOCK_ROOT", tmp_path / "absent")
    monkeypatch.setattr(storage_probe.sys, "platform", "linux")
    locations = InspectDataStorage(settings, storage_probe.StorageProbe())
    ReportStorageProfiles(settings, locations)
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.count("[存储配置需修改]") == 1
    assert "config/runtime.json" in output.err
    assert "storage.overrides" in output.err and '"kind": "hdd"' in output.err
    assert "data/training/samples/v5" in output.err
    assert "ssd" in output.err and "重新启动" in output.err
    assert len(locations) == 23
    assert not settings.data_paths.root.exists()
    raw["storage"] = {"overrides": [{"path": "data", "kind": "ssd"}]}
    path.write_text(json.dumps(raw))
    configured = LoadRuntimeSettings(path)
    assert configured.storage_overrides == (StorageOverride(tmp_path / "data", "ssd"),)
    all_manual = replace(configured, storage_overrides=(StorageOverride(tmp_path, "ssd"),))
    ReportStorageProfiles(
        all_manual, InspectDataStorage(all_manual, storage_probe.StorageProbe(all_manual.storage_overrides))
    )
    output = capsys.readouterr()
    assert "已使用手动配置" in output.err and "需修改" not in output.err


def test_platform_timeout_becomes_actionable_unknown(tmp_path, monkeypatch):
    def timed_out(path, *, timeout):
        assert timeout == 3.0
        raise subprocess.TimeoutExpired("probe", timeout)

    monkeypatch.setattr(storage_probe.sys, "platform", "win32")
    monkeypatch.setattr(storage_probe, "ProbeWindowsStorage", timed_out)
    profile = storage_probe.StorageProbe().inspect(tmp_path)
    assert profile.kind == "unknown" and profile.reason == "probe_timeout"


def test_windows_path_is_data_not_interpolated_code(tmp_path, monkeypatch):
    def run(argv, **kwargs):
        assert argv[:3] == ["powershell.exe", "-NoProfile", "-NonInteractive"]
        assert str(tmp_path) not in argv[-1]
        assert kwargs["env"]["TRADER_STORAGE_PROBE_PATH"].startswith(str(tmp_path))
        assert kwargs["timeout"] == 3.0 and kwargs["check"] is True
        return subprocess.CompletedProcess(argv, 0, b"ssd\r\n")

    monkeypatch.setattr(storage_platform.subprocess, "run", run)
    assert storage_platform.ProbeWindowsStorage(tmp_path, timeout=3.0) == "ssd"


def test_mac_apfs_resolves_all_physical_stores(tmp_path, monkeypatch):
    replies = iter(
        [
            {"APFSContainerReference": "disk3", "SolidState": True},
            {"Containers": [{"PhysicalStores": [{"DeviceIdentifier": "disk0s2"}, {"DeviceIdentifier": "disk1s2"}]}]},
            {"SolidState": True},
            {"SolidState": False},
        ]
    )

    def run(argv, **kwargs):
        assert 0 < kwargs["timeout"] <= 3.0
        assert argv[0] == "/usr/sbin/diskutil"
        return subprocess.CompletedProcess(argv, 0, plistlib.dumps(next(replies)))

    monkeypatch.setattr(storage_platform.subprocess, "run", run)
    assert storage_platform.ProbeMacStorage(tmp_path, timeout=3.0) == "hdd"
