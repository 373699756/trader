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
        "configuration_field": "storage.overrides",
        "override_examples": [{"path": "data/training/samples", "kind": "hdd"}],
    }
    report = build_report(
        "storage", [DiagnosticResult("storage_profile", 0, 1.0, {"status": "degraded", "summary": summary}, None)]
    )
    assert report["status"] == "degraded"
    assert report["checks"][0]["summary"] == summary


def test_storage_command_reports_real_config_and_external_paths(tmp_path, monkeypatch, capsys):
    import json
    import sys

    from trader.infra import storage_probe

    config = Path(__file__).resolve().parents[3] / "config/runtime.json"
    monkeypatch.setattr(storage_probe, "_SYSFS_BLOCK_ROOT", tmp_path / "missing")
    monkeypatch.setattr(sys, "argv", ["storage", "--config", str(config), "--path", str(tmp_path / "new-output")])
    assert storage_profile.Main() == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["status"] == "degraded"
    assert report["summary"]["configuration_field"] == "storage.overrides"
    assert report["summary"]["locations"][-1]["path"] == str(tmp_path / "new-output")
    assert report["summary"]["override_examples"]
    assert "存储配置需修改" in output.err
    assert not (tmp_path / "new-output").exists()
