from pathlib import Path

from scripts.diagnose_runtime import _parser, _validate, build_commands, build_report
from scripts.runtime_diagnostics import storage_profile
from scripts.runtime_diagnostics.diagnostic_types import DiagnosticResult


def test_storage_profile_routes_extra_paths_and_preserves_actionable_details(tmp_path: Path) -> None:
    options, _ = _validate(_parser().parse_args(["--profile", "storage", "--storage-path", str(tmp_path)]))
    commands = build_commands(options, python_executable="/python3")
    assert len(commands) == 1
    assert commands[0].argv[-2:] == ("--path", str(tmp_path))
    summary = {
        "locations": [{"path": "data/training/samples", "kind": "unknown"}],
        "configuration_file": "config/custom-runtime.json",
        "configuration_field": "storage",
        "configuration_example": {"storage": "hdd"},
    }
    report = build_report(
        "storage", [DiagnosticResult("storage_profile", 0, 1.0, {"status": "degraded", "summary": summary}, None)]
    )
    assert report["status"] == "degraded"
    assert report["checks"][0]["summary"] == summary


def test_storage_command_reports_real_config_and_external_paths(tmp_path, monkeypatch, capsys):
    import json
    import shutil
    import sys

    from trader.infra import storage_probe

    config_dir = tmp_path / "config"
    shutil.copytree(Path(__file__).resolve().parents[3] / "config", config_dir)
    config = config_dir / "runtime.json"
    raw = json.loads(config.read_text(encoding="utf-8"))
    raw["storage"] = None
    config.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(storage_probe, "_SYSFS_BLOCK_ROOT", tmp_path / "missing")
    monkeypatch.setattr(sys, "argv", ["storage", "--config", str(config), "--path", str(tmp_path / "new-output")])
    assert storage_profile.Main() == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["status"] == "degraded"
    assert report["summary"]["configuration_field"] == "storage"
    assert report["summary"]["locations"][-1]["path"] == "new-output"
    assert report["summary"]["configuration_example"] == {"storage": "hdd"}
    assert "存储配置需修改" in output.err
    assert not (tmp_path / "new-output").exists()


def test_storage_command_degrades_when_configured_medium_disagrees_with_physical(
    tmp_path, monkeypatch, capsys
):
    import json
    import shutil
    import sys

    from trader.infra.storage_types import StorageProfile

    class _StorageProbe:
        def __init__(self, medium=None):
            self._medium = medium

        def inspect(self, path):
            if self._medium is not None:
                return StorageProfile(self._medium, "configured", "configuration", "manual_medium")
            return StorageProfile("hdd", "sda", "automatic", "rotational_metadata")

    config_dir = tmp_path / "config"
    shutil.copytree(Path(__file__).resolve().parents[3] / "config", config_dir)
    config = config_dir / "runtime.json"
    raw = json.loads(config.read_text(encoding="utf-8"))
    raw["storage"] = "ssd"
    config.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(storage_profile, "StorageProbe", _StorageProbe)
    monkeypatch.setattr(sys, "argv", ["storage", "--config", str(config)])

    assert storage_profile.Main() == 0

    output = capsys.readouterr()
    report = json.loads(output.out)
    summary = report["summary"]
    assert report["status"] == "degraded"
    assert summary["configured_mismatches"] == len(summary["locations"])
    assert summary["configuration_example"] == {"storage": "hdd"}
    assert all(item["kind"] == "ssd" for item in summary["locations"])
    assert all(item["actual_kind"] == "hdd" for item in summary["locations"])
    assert all(item["configured_mismatch"] for item in summary["locations"])
    assert "存储配置需修改" in output.err


def test_storage_command_does_not_treat_unverified_manual_medium_as_performance_evidence(
    tmp_path, monkeypatch, capsys
):
    import json
    import shutil
    import sys

    from trader.infra.storage_types import StorageProfile

    class _StorageProbe:
        def __init__(self, medium=None):
            self._medium = medium

        def inspect(self, path):
            if self._medium is not None:
                return StorageProfile(self._medium, "configured", "configuration", "manual_medium")
            return StorageProfile("unknown", "virtual", "automatic", "device_metadata_unavailable")

    config_dir = tmp_path / "config"
    shutil.copytree(Path(__file__).resolve().parents[3] / "config", config_dir)
    config = config_dir / "runtime.json"
    monkeypatch.setattr(storage_profile, "StorageProbe", _StorageProbe)
    monkeypatch.setattr(sys, "argv", ["storage", "--config", str(config)])

    assert storage_profile.Main() == 0

    report = json.loads(capsys.readouterr().out)
    summary = report["summary"]
    assert report["status"] == "degraded"
    assert summary["configured_mismatches"] == 0
    assert summary["actual_unknown"] == len(summary["locations"])
    assert summary["configuration_example"] is None
    assert "不能作为磁盘性能证据" in summary["instruction"]
