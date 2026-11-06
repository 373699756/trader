"""Public selection and process failures must not produce false acceptance."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.core.catalog import TOOLS
from scripts.core.catalog import SelectTool
from scripts.core.catalog import SelectionArguments
from scripts.core.cli import ParseArguments
from scripts.core.contracts import DiagnosticCommand
from scripts.core.contracts import DiagnosticResult
from scripts.core.execution import ExecuteCommand
from scripts.core.execution import RunDiagnostics
from scripts.core.projections import BuildReport, NormalizeActionResult, ResultStatus

ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.parametrize("option", ("--help", "--list"))
def test_discovery_does_not_import_business_workers(option: str) -> None:
    code = (
        "import contextlib,io,sys; from scripts.core.cli import Main; "
        f"sys.argv=['diagnose', {option!r}]; "
        "\ntry:\n with contextlib.redirect_stdout(io.StringIO()): Main()"
        "\nexcept SystemExit as error:\n assert error.code == 0"
        "\nassert not any(name.startswith(('scripts.training.', 'scripts.download.company.', 'trader.training.', 'trader.bootstrap')) for name in sys.modules)"
    )
    subprocess.run((sys.executable, "-c", code), cwd=ROOT, check=True, capture_output=True, timeout=15)


def test_catalogue_has_unique_complete_selections() -> None:
    keys = {(item.business, item.module, item.action) for item in TOOLS}
    assert len(keys) == len(TOOLS)
    for item in TOOLS:
        assert SelectTool(item.business, item.module, item.action) is item


@pytest.mark.parametrize("status,expected", (
    ("passed", 0), ("degraded", 0), ("empty", 0), ("failed", 1),
    ("skipped", 1), ("pending", 1), ("timed_out", 1), ("cancelled", 1),
))
def test_foreground_exit_code_cannot_claim_success_for_an_incomplete_result(monkeypatch, capsys, status, expected) -> None:
    from scripts.core.cli import Main
    from scripts.core.contracts import ToolOutcome
    from scripts.recommendation.assets import cli

    monkeypatch.setattr(cli, "Run", lambda *_args: ToolOutcome(0, {"status": status}))
    assert Main(list(SelectionArguments("assets"))) == expected
    report = json.loads(capsys.readouterr().out)
    assert report["status"] in ({"passed", "degraded", "empty"} if expected == 0 else {"failed", "degraded"})


@pytest.mark.parametrize("arguments", (("--list", "--unknown-tool-option"), ("--help", "--unknown-tool-option")))
def test_global_discovery_rejects_unknown_parameters(arguments: tuple[str, ...]) -> None:
    completed = subprocess.run(
        (sys.executable, str(ROOT / "scripts/tools.py"), *arguments),
        cwd=ROOT, capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 2
    assert "unknown-tool-option" in completed.stderr


def test_selected_help_exposes_only_its_business_inputs() -> None:
    completed = subprocess.run(
        (sys.executable, str(ROOT / "scripts/tools.py"), *SelectionArguments("web"), "--help"),
        cwd=ROOT, capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--base-url" in completed.stdout
    assert "--report" in completed.stdout
    assert "--company-replay-directory" not in completed.stdout
    assert "--training-probe-directory" not in completed.stdout


def test_local_normalization_keeps_the_selected_profile_in_the_result_name() -> None:
    result = DiagnosticResult("configuration:v2", 0, 0, {"status": "ok"}, None)
    normalized = NormalizeActionResult("snapshot", result)
    assert normalized.name == "configuration:v2"
    assert ResultStatus(normalized) == "passed"
    report = BuildReport("snapshot", (result,))
    assert report["checks"][0]["name"] == "configuration:v2"
    assert report["checks"][0]["status"] == "passed"


def test_web_report_only_loads_its_selected_reporter(monkeypatch) -> None:
    from scripts.core import projections

    loaded = []
    original = projections.importlib.import_module
    def imported(name):
        loaded.append(name)
        return original(name)
    monkeypatch.setattr(projections.importlib, "import_module", imported)
    report = BuildReport("web", (DiagnosticResult("web_health", 0, 0, {"status": "passed"}, None),))
    assert report["status"] == "passed"
    assert set(loaded) == {"scripts.recommendation.web.reporting"}


@pytest.mark.parametrize("kind", ("configuration", "directory", "symlink"))
def test_public_report_rejects_existing_inputs_without_changing_them(tmp_path, kind: str) -> None:
    configuration = tmp_path / "runtime.json"
    configuration.write_bytes(b'{"fixture":true}\n')
    target = configuration
    if kind == "directory":
        target = tmp_path / "reports"
        target.mkdir()
    elif kind == "symlink":
        target = tmp_path / "report-link.json"
        target.symlink_to(configuration)
    with pytest.raises(SystemExit) as error:
        ParseArguments([*SelectionArguments("snapshot"), "--runtime-config", str(configuration), "--report", str(target)])
    assert error.value.code == 2
    assert configuration.read_bytes() == b'{"fixture":true}\n'
    assert target.is_symlink() if kind == "symlink" else target.exists()


def test_memory_artifact_and_public_report_require_distinct_files(tmp_path) -> None:
    target = str(tmp_path / "report.json")
    with pytest.raises(SystemExit) as error:
        ParseArguments([*SelectionArguments("memory"), "--report", target, "--", "--output", target])
    assert error.value.code == 2
    assert not Path(target).exists()


def test_long_evidence_and_public_report_require_distinct_files(tmp_path) -> None:
    target = str(tmp_path / "evidence.json")
    with pytest.raises(SystemExit) as error:
        ParseArguments([*SelectionArguments("long-watchlist"), "--report", target, "--long-evidence-output", target])
    assert error.value.code == 2
    assert not Path(target).exists()


@pytest.mark.parametrize("identity,option", (
    ("training-alignment", "--training-probe-directory"),
    ("company-resume", "--company-replay-directory"),
    ("history-repack-build", "--target"),
    ("migration", "--source"),
    ("migration-build", "--source"),
    ("migration-build", "--target"),
    ("migration-rollback", "--backup"),
    ("migration-rollback", "--target"),
))
def test_public_report_stays_outside_explicit_business_output_directories(tmp_path, identity, option) -> None:
    output = tmp_path / "business-output"
    with pytest.raises(SystemExit) as error:
        ParseArguments([*SelectionArguments(identity), "--report", str(output / "report.json"), option, str(output)])
    assert error.value.code == 2
    assert not output.exists()


def test_public_report_rejects_a_file_parent_before_running_business(tmp_path) -> None:
    blocker = tmp_path / "existing-input"
    blocker.write_bytes(b"fixture input")
    with pytest.raises(SystemExit) as error:
        ParseArguments([*SelectionArguments("snapshot"), "--report", str(blocker / "nested" / "report.json")])
    assert error.value.code == 2
    assert blocker.read_bytes() == b"fixture input"


@pytest.mark.parametrize("suffix", ("report.json", "nested/report.json"))
def test_skill_artifact_directory_cannot_contain_the_public_report(tmp_path, suffix: str) -> None:
    output = tmp_path / "skill-results"
    with pytest.raises(SystemExit) as error:
        ParseArguments([
            *SelectionArguments("skill-evaluation"), "--report", str(output / suffix),
            "--", "run", "--output-dir", str(output),
        ])
    assert error.value.code == 2
    assert not output.exists()


@pytest.mark.parametrize("command", ("run", "compare"))
def test_skill_parser_rejects_abbreviations_without_evaluation_or_report_reads(tmp_path, monkeypatch, command: str) -> None:
    from scripts.engineering.skills import cli

    monkeypatch.setattr(cli, "_run", lambda _args: pytest.fail("paid evaluation must not start"))
    monkeypatch.setattr(cli, "CompareReports", lambda *_args: pytest.fail("existing reports must not be read"))
    arguments = ["--out", str(tmp_path / "outputs")] if command == "run" else [
        "--bef", str(tmp_path / "before"), "--after", str(tmp_path / "after"),
    ]
    monkeypatch.setattr(sys, "argv", ["skills", command, *arguments])
    with pytest.raises(SystemExit) as error:
        cli.Main()
    assert error.value.code == 2


@pytest.mark.parametrize(
    ("identity", "argument"),
    (("migration-build", "--target"), ("migration-rollback", "--backup"), ("history-repack-build", "--target")),
)
def test_public_report_cannot_be_an_ancestor_of_a_maintenance_directory(tmp_path, identity: str, argument: str) -> None:
    report = tmp_path / "new-report"
    with pytest.raises(SystemExit) as error:
        ParseArguments([*SelectionArguments(identity), "--report", str(report), "--", argument, str(report / "nested")])
    assert error.value.code == 2
    assert not report.exists()


def test_memory_parser_rejects_abbreviated_output_before_configuring_resources(tmp_path, monkeypatch) -> None:
    from scripts.training.resources import memory

    monkeypatch.setattr(memory, "_configure_resources", lambda: pytest.fail("training resources must remain untouched"))
    with pytest.raises(SystemExit) as error:
        memory.Main([
            "--history-root", str(tmp_path / "history"), "--train-root", str(tmp_path / "models"),
            "--sample-root", str(tmp_path / "samples"), "--out", str(tmp_path / "report.json"),
            "--expected-history-snapshot-hash", "a" * 64,
        ])
    assert error.value.code == 2


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


@pytest.mark.parametrize("definition", tuple(item for item in TOOLS if item.worker is not None), ids=lambda item: item.identity)
def test_native_help_exposes_inputs_without_running_the_check(definition) -> None:
    completed = subprocess.run(
        (sys.executable, str(ROOT / "scripts/tools.py"), *SelectionArguments(definition.identity), "--", "--help"),
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
        "snapshot", (DiagnosticCommand("configuration", (), 1), DiagnosticCommand("history_maintenance", (), 1, requires_success=("configuration",))),
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
    from scripts.core import execution
    from scripts.core.process import DiagnosticOutputLimitError

    def overflow(*_args, **_kwargs):
        raise DiagnosticOutputLimitError("fixture limit")

    monkeypatch.setattr(execution, "RunBoundedCommand", overflow)
    result = ExecuteCommand(DiagnosticCommand("probe", (), 1))
    assert result.error_code == "command_output_limit"


def test_selected_naming_audit_preserves_the_reviewable_report() -> None:
    completed = subprocess.run(
        (sys.executable, str(ROOT / "scripts/tools.py"), *SelectionArguments("quality-transform"),
         "--", "--naming-audit", "scripts/tools.py", "--json"),
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    audit = json.loads(report["checks"][0]["summary"]["tool_output"])
    assert audit["findings"] == []
    assert audit["manifest_template"]["files"][0]["path"] == "scripts/tools.py"


def test_current_harness_can_run_from_an_older_checkout(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/tools.py").write_text("raise RuntimeError('old entrypoint called')\n")
    completed = subprocess.run(
        (sys.executable, str(ROOT / "scripts/tools.py"), "--internal-worker",
         "scripts.training.jobs.training_alignment", "--", "--help"),
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--history-root" in completed.stdout


@pytest.mark.parametrize("option", ("--history-root", "--train-root", "--sample-root", "--output"))
def test_memory_check_rejects_repository_paths_before_configuring_resources(tmp_path, monkeypatch, option) -> None:
    from scripts.training.resources import memory

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
        "snapshot", (DiagnosticCommand("configuration", (), 1), DiagnosticCommand("history_maintenance", (), 1, requires_success=("configuration",))),
        runner=runner,
    )
    assert [check["status"] for check in report["checks"]] == [status, "skipped"]


def test_skipped_flag_cannot_hide_a_worker_failure() -> None:
    report = BuildReport("disk", (
        DiagnosticResult("probe", 1, 1, {"status": "skipped", "executed": False}, None),
    ))
    assert report["status"] == "failed"
    assert report["checks"][0]["executed"] is True
