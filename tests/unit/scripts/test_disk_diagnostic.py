from pathlib import Path

from scripts.diagnose_runtime import _parser, _validate, build_commands, build_report
from scripts.runtime_diagnostics import disk_profile
from scripts.runtime_diagnostics.diagnostic_types import DiagnosticResult
from scripts.runtime_diagnostics.disk_policy_audit import AuditSqlitePolicyConsumers


def test_policy_audit_reports_missing_and_unpacked_injection_without_touching_source(tmp_path):
    source = tmp_path / "src/trader/download/infra/example.py"
    source.parent.mkdir(parents=True)
    content = (
        "from trader.infra.sqlite_connection import SqliteSession as Session\n"
        "Session(path, sqlite_policy=policy)\n"
        "SqliteSession(path)\n"
        "OpenSqliteConnection(path, **arguments)\n"
    )
    source.write_text(content)
    report = AuditSqlitePolicyConsumers(tmp_path)
    assert report["connection_calls"] == 3
    assert report["explicit_policy_calls"] == 1
    assert report["missing_policy_calls"][0]["line"] == 3
    assert report["unpacked_policy_calls_require_review"][0]["line"] == 4
    assert source.read_text() == content


def test_policy_audit_is_explicitly_routed_to_the_disk_worker():
    options, _ = _validate(_parser().parse_args(["--profile", "disk", "--disk-policy-audit"]))
    command = build_commands(options, python_executable="/python3")[0]
    assert command.argv[-1] == "--policy-audit"


def test_disk_profile_routes_extra_paths_and_preserves_actionable_details(tmp_path: Path) -> None:
    options, _ = _validate(_parser().parse_args(["--profile", "disk", "--disk-path", str(tmp_path)]))
    commands = build_commands(options, python_executable="/python3")
    assert len(commands) == 1
    assert commands[0].argv[-2:] == ("--path", str(tmp_path))
    summary = {
        "locations": [{"path": "data/training/samples", "kind": "unknown"}],
        "configuration_file": "config/custom-runtime.json",
        "configuration_field": "disk_medium",
        "configuration_example": {"disk_medium": "hdd"},
    }
    report = build_report(
        "disk", [DiagnosticResult("disk_profile", 0, 1.0, {"status": "degraded", "summary": summary}, None)]
    )
    assert report["status"] == "degraded"
    assert report["checks"][0]["summary"] == summary


def test_disk_command_reports_real_config_and_external_paths(tmp_path, monkeypatch, capsys):
    import json
    import shutil
    import sys

    from trader.infra import disk_probe

    config_dir = tmp_path / "config"
    shutil.copytree(Path(__file__).resolve().parents[3] / "config", config_dir)
    config = config_dir / "runtime.json"
    raw = json.loads(config.read_text(encoding="utf-8"))
    raw["disk_medium"] = None
    config.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(disk_probe, "_SYSFS_BLOCK_ROOT", tmp_path / "missing")
    monkeypatch.setattr(sys, "argv", ["disk", "--config", str(config), "--path", str(tmp_path / "new-output")])
    assert disk_profile.Main() == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["status"] == "degraded"
    assert report["summary"]["configuration_field"] == "disk_medium"
    assert report["summary"]["locations"][-1]["path"] == "new-output"
    assert report["summary"]["configuration_example"] == {"disk_medium": "hdd"}
    assert "存储配置需修改" in output.err
    assert not (tmp_path / "new-output").exists()


def test_disk_command_degrades_when_configured_medium_disagrees_with_physical(
    tmp_path, monkeypatch, capsys
):
    import json
    import shutil
    import sys

    from trader.infra.disk_types import DiskProfile

    class _DiskProbe:
        def __init__(self, medium=None):
            self._medium = medium

        def inspect(self, path):
            if self._medium is not None:
                return DiskProfile(self._medium, "configured", "configuration", "manual_medium")
            return DiskProfile("hdd", "sda", "automatic", "rotational_metadata")

    config_dir = tmp_path / "config"
    shutil.copytree(Path(__file__).resolve().parents[3] / "config", config_dir)
    config = config_dir / "runtime.json"
    raw = json.loads(config.read_text(encoding="utf-8"))
    raw["disk_medium"] = "ssd"
    config.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(disk_profile, "DiskProbe", _DiskProbe)
    monkeypatch.setattr(sys, "argv", ["disk", "--config", str(config)])

    assert disk_profile.Main() == 0

    output = capsys.readouterr()
    report = json.loads(output.out)
    summary = report["summary"]
    assert report["status"] == "degraded"
    assert summary["configured_mismatches"] == len(summary["locations"])
    assert summary["configuration_example"] == {"disk_medium": "hdd"}
    assert all(item["kind"] == "ssd" for item in summary["locations"])
    assert all(item["actual_kind"] == "hdd" for item in summary["locations"])
    assert all(item["configured_mismatch"] for item in summary["locations"])
    assert "存储配置需修改" in output.err


def test_disk_command_does_not_treat_unverified_manual_medium_as_performance_evidence(
    tmp_path, monkeypatch, capsys
):
    import json
    import shutil
    import sys

    from trader.infra.disk_types import DiskProfile

    class _DiskProbe:
        def __init__(self, medium=None):
            self._medium = medium

        def inspect(self, path):
            if self._medium is not None:
                return DiskProfile(self._medium, "configured", "configuration", "manual_medium")
            return DiskProfile("unknown", "virtual", "automatic", "device_metadata_unavailable")

    config_dir = tmp_path / "config"
    shutil.copytree(Path(__file__).resolve().parents[3] / "config", config_dir)
    config = config_dir / "runtime.json"
    monkeypatch.setattr(disk_profile, "DiskProbe", _DiskProbe)
    monkeypatch.setattr(sys, "argv", ["disk", "--config", str(config)])

    assert disk_profile.Main() == 0

    report = json.loads(capsys.readouterr().out)
    summary = report["summary"]
    assert report["status"] == "degraded"
    assert summary["configured_mismatches"] == 0
    assert summary["actual_unknown"] == len(summary["locations"])
    assert summary["configuration_example"] is None
    assert "不能作为磁盘性能证据" in summary["instruction"]
