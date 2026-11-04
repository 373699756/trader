from __future__ import annotations

import json
import shutil
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from trader import bootstrap
from trader.entrypoints import cli, server
from trader.infra import disk_probe, disk_runtime
from trader.infra.settings import LoadRuntimeSettings
from trader.infra.disk_types import DiskLocation, DiskProfile

ROOT = Path(__file__).resolve().parents[3]


class _Terminal(StringIO):
    def isatty(self) -> bool:
        return True


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "config"
    shutil.copytree(ROOT / "config", config)
    path = config / "runtime.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["disk_medium"] = None
    path.write_text(json.dumps(raw), encoding="utf-8")
    paths = ("runtime", "history")

    def inspect(settings, probe, *, additional_paths=()):
        assert additional_paths == ()
        return tuple(
            DiskLocation(role, path, probe.inspect(path))
            for role, path in zip(paths, (settings.runtime_dir, settings.data_paths.history), strict=True)
        )

    monkeypatch.setattr(disk_runtime, "InspectDataDisk", inspect)
    monkeypatch.setattr(
        disk_probe,
        "_probe_device",
        lambda _path, device: DiskProfile(
            "unknown", f"fixture:{device}", "automatic", "device_metadata_unavailable"
        ),
    )
    return path


@pytest.mark.parametrize(("answer", "kind"), [("1\n", "ssd"), ("2\n", "hdd")])
def test_interactive_disk_choice_is_saved_and_reused(tmp_path, monkeypatch, answer, kind):
    path = _config(tmp_path, monkeypatch)
    monkeypatch.setattr(disk_runtime.sys, "stdin", _Terminal(answer))
    first = bootstrap.PrepareRuntimeDisk(LoadRuntimeSettings(path), choose=True, report=False)

    assert first.disk_medium == kind
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["data_dir"] == "data"
    assert raw["disk_medium"] == kind

    monkeypatch.setattr(disk_runtime.sys, "stdin", _Terminal(""))
    second = bootstrap.PrepareRuntimeDisk(LoadRuntimeSettings(path), choose=True, report=False)
    assert second.disk_medium == first.disk_medium
    assert json.loads(path.read_text(encoding="utf-8")) == raw


def test_noninteractive_disk_probe_does_not_change_configuration(tmp_path, monkeypatch):
    path = _config(tmp_path, monkeypatch)
    before = path.read_bytes()
    monkeypatch.setattr(disk_runtime.sys, "stdin", StringIO("1\n"))

    settings = LoadRuntimeSettings(path)
    assert bootstrap.PrepareRuntimeDisk(settings, choose=True, report=False) is settings
    assert path.read_bytes() == before


def test_unresolved_paths_prompt_once(tmp_path):
    unknown = DiskProfile("unknown", "unresolved", "automatic", "path_unavailable")
    first = tmp_path / "first"
    second = tmp_path / "second"
    locations = (DiskLocation("first", first, unknown), DiskLocation("second", second, unknown))

    assert disk_runtime.PromptUnknownDisk(locations, input_stream=_Terminal("1\n2\n")) == "ssd"


def test_configured_medium_skips_prompt(tmp_path, monkeypatch):
    path = _config(tmp_path, monkeypatch)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["disk_medium"] = "ssd"
    path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(disk_runtime.sys, "stdin", _Terminal("2\n"))

    selected = bootstrap.PrepareRuntimeDisk(LoadRuntimeSettings(path), choose=True, report=False)
    assert selected.disk_medium == "ssd"
    assert json.loads(path.read_text(encoding="utf-8"))["disk_medium"] == "ssd"


def test_train_entrypoint_saves_choice_before_running(tmp_path, monkeypatch):
    path = _config(tmp_path, monkeypatch)
    monkeypatch.setattr(disk_runtime.sys, "stdin", _Terminal("1\n"))

    def train(runtime, profiles):
        assert runtime.disk_medium == "ssd"
        assert profiles == ("v2",)
        return 0

    monkeypatch.setattr(cli, "_run_selected_training", train)

    assert cli.Main(["--config", str(path), "train", "v2"]) == 0
    assert LoadRuntimeSettings(path).disk_medium == "ssd"


def test_server_entrypoint_saves_choice_before_build(tmp_path, monkeypatch):
    path = _config(tmp_path, monkeypatch)
    monkeypatch.setattr(disk_runtime.sys, "stdin", _Terminal("2\n"))

    def build(_path, *, scoring_profile):
        assert scoring_profile == "v2"
        assert LoadRuntimeSettings(path).disk_medium == "hdd"
        return SimpleNamespace(settings=SimpleNamespace(pipeline=SimpleNamespace(shutdown_timeout_seconds=30)))

    monkeypatch.setattr(bootstrap, "BuildSystem", build)
    monkeypatch.setattr(server, "_run_system", lambda _system, *, timeout_seconds: 0)

    assert server.Main(["--config", str(path), "v2"]) == 0
