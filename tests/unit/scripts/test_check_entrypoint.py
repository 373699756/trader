"""Public selection and process failures must not produce false acceptance."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.checks.catalog import CHECKS, SelectCheck, SelectionArguments
from scripts.checks.contracts import DiagnosticCommand, DiagnosticResult
from scripts.checks.execution import ExecuteCommand, RunDiagnostics
from scripts.checks.projections import BuildReport

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("option", ("--help", "--list"))
def test_discovery_does_not_import_business_workers(option: str) -> None:
    code = (
        "import contextlib,io,sys; from scripts.checks.cli import Main; "
        f"sys.argv=['diagnose', {option!r}]; "
        "\ntry:\n with contextlib.redirect_stdout(io.StringIO()): Main()"
        "\nexcept SystemExit as error:\n assert error.code == 0"
        "\nassert not any(name.startswith(('scripts.checks.training.', 'scripts.checks.download.company.', 'trader.training.', 'trader.bootstrap')) for name in sys.modules)"
    )
    subprocess.run((sys.executable, "-c", code), cwd=ROOT, check=True, capture_output=True, timeout=15)


def test_catalogue_has_unique_complete_selections() -> None:
    keys = {(item.business, item.module, item.check) for item in CHECKS}
    assert len(keys) == len(CHECKS)
    for item in CHECKS:
        assert SelectCheck(item.business, item.module, item.check) is item


@pytest.mark.parametrize("status", ("failed", "timed_out", "cancelled", "skipped", "empty"))
def test_distinct_result_states_reach_report(status: str) -> None:
    error = {"timed_out": "command_timeout", "cancelled": "command_cancelled"}.get(status)
    result = DiagnosticResult("probe", 0, 1.0, {"status": status}, error)
    report = BuildReport("disk", (result,))
    assert report["checks"][0]["status"] == status
    assert report["summary"][status] == 1
    assert report["status"] != "passed"


def test_selected_text_tool_reports_nonzero_without_forwarding_raw_output() -> None:
    command = DiagnosticCommand(
        "quality-debt", (sys.executable, "-c", "print('private payload'); raise SystemExit(3)"), 5,
        output_kind="text",
    )
    result = ExecuteCommand(command)
    report = BuildReport("quality-debt", (result,))
    assert report["status"] == "failed"
    assert "private payload" not in json.dumps(report)
    assert report["checks"][0]["summary"]["exit_code"] == 3


def test_deadline_is_reported_as_timeout() -> None:
    result = ExecuteCommand(DiagnosticCommand("probe", (sys.executable, "-c", "import time; time.sleep(30)"), 0.1))
    assert result.error_code == "command_timeout"
    assert BuildReport("disk", (result,))["checks"][0]["status"] == "timed_out"


def test_empty_payload_cannot_be_reported_as_success() -> None:
    result = ExecuteCommand(DiagnosticCommand("probe", (sys.executable, "-c", "pass"), 5))
    assert result.error_code == "invalid_json"
    assert BuildReport("disk", (result,))["status"] == "failed"


@pytest.mark.parametrize("definition", tuple(item for item in CHECKS if item.worker is not None), ids=lambda item: item.identity)
def test_native_help_exposes_inputs_without_running_the_check(definition) -> None:
    completed = subprocess.run(
        (sys.executable, str(ROOT / "scripts/diagnose_runtime.py"), *SelectionArguments(definition.identity), "--", "--help"),
        cwd=ROOT, capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout
    assert "trader-runtime-diagnostics" not in completed.stdout


@pytest.mark.parametrize("return_code", (0, 1, 130))
def test_worker_reported_cancellation_stops_remaining_selected_commands(return_code: int) -> None:
    calls: list[str] = []

    def runner(command: DiagnosticCommand) -> DiagnosticResult:
        calls.append(command.name)
        return DiagnosticResult(command.name, return_code, 1.0, {"status": "cancelled"}, None)

    report = RunDiagnostics(
        "snapshot", (DiagnosticCommand("configuration", (), 1), DiagnosticCommand("history_maintenance", (), 1)),
        runner=runner,
    )
    assert calls == ["configuration"]
    assert [check["status"] for check in report["checks"]] == ["cancelled", "skipped"]
    assert report["status"] == "failed"


@pytest.mark.parametrize("status", ([], {}, True, "unknown"))
def test_malformed_worker_status_cannot_prove_success(status: object) -> None:
    report = BuildReport("disk", (DiagnosticResult("probe", 0, 1.0, {"status": status}, None),))
    assert report["status"] == "failed"


def test_output_limit_has_its_own_failure_category(monkeypatch) -> None:
    from scripts.checks import execution
    from scripts.checks.infra.database_workspace import DiagnosticOutputLimitError

    def overflow(*_args, **_kwargs):
        raise DiagnosticOutputLimitError("fixture limit")

    monkeypatch.setattr(execution, "RunDatabaseProbe", overflow)
    result = ExecuteCommand(DiagnosticCommand("probe", (), 1))
    assert result.error_code == "command_output_limit"


def test_selected_naming_audit_preserves_the_reviewable_report() -> None:
    completed = subprocess.run(
        (sys.executable, str(ROOT / "scripts/diagnose_runtime.py"), *SelectionArguments("quality-transform"),
         "--", "--naming-audit", "scripts/diagnose_runtime.py", "--json"),
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    audit = json.loads(report["checks"][0]["summary"]["tool_output"])
    assert audit["findings"] == []
    assert audit["manifest_template"]["files"][0]["path"] == "scripts/diagnose_runtime.py"


def test_current_harness_can_run_from_an_older_checkout(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/diagnose_runtime.py").write_text("raise RuntimeError('old entrypoint called')\n")
    completed = subprocess.run(
        (sys.executable, str(ROOT / "scripts/diagnose_runtime.py"), "--internal-worker",
         "scripts.checks.training.training_alignment", "--", "--help"),
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--history-root" in completed.stdout


@pytest.mark.parametrize("option", ("--history-root", "--train-root", "--sample-root", "--output"))
def test_memory_check_rejects_repository_paths_before_configuring_resources(tmp_path, monkeypatch, option) -> None:
    from scripts.checks.training import memory

    def configured():
        raise AssertionError("resources configured before input validation")

    monkeypatch.setattr(memory, "_configure_resources", configured)
    arguments = {
        "--history-root": str(tmp_path / "history"), "--train-root": str(tmp_path / "models"),
        "--sample-root": str(tmp_path / "samples"), "--output": str(tmp_path / "report.json"),
        "--expected-history-snapshot-hash": "a" * 64,
    }
    arguments[option] = str(ROOT / "data")
    with pytest.raises(SystemExit) as error:
        memory.Main([part for pair in arguments.items() for part in pair])
    assert error.value.code == 2


@pytest.mark.parametrize("identity,field", (("memory", "peak_rss_bytes"), ("wheel-install", "resource_count")))
def test_native_reports_keep_their_public_evidence(identity: str, field: str) -> None:
    report = BuildReport(identity, (DiagnosticResult(identity, 0, 1, {"status": "passed", field: 10, "private": "omit"}, None),))
    assert report["checks"][0]["summary"] == {field: 10}



@pytest.mark.parametrize("status", ("failed", "timed_out", "empty"))
def test_invalid_configuration_skips_the_dependent_snapshot_checks(status: str) -> None:
    def runner(command: DiagnosticCommand) -> DiagnosticResult:
        assert command.name == "configuration"
        return DiagnosticResult(command.name, 0, 1, {"status": status}, None)

    report = RunDiagnostics(
        "snapshot", (DiagnosticCommand("configuration", (), 1), DiagnosticCommand("history_maintenance", (), 1)),
        runner=runner,
    )
    assert [check["status"] for check in report["checks"]] == [status, "skipped"]


def test_skipped_flag_cannot_hide_a_worker_failure() -> None:
    report = BuildReport("disk", (
        DiagnosticResult("probe", 1, 1, {"status": "skipped", "executed": False}, None),
    ))
    assert report["status"] == "failed"
    assert report["checks"][0]["executed"] is True
