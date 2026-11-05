"""The foreground live-sync adapter is tested without invoking the real sync."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.core.paths import PROJECT_ROOT
from scripts.download.company import live_sync


def _measurement(code: int | None = 0, *, status: str = "passed", cancelled: bool = False) -> dict[str, object]:
    return {
        "schema_version": "company-live-sync-measurement",
        "status": status,
        "cancelled": cancelled,
        "cli_exit_code": code,
        "writes_current_company_data": True,
        "isolated_sample": False,
        "field_correctness_verified": False,
        "database_statistics_final": True,
        "status_basis": "cli_exit_code_only",
        "private": "must-not-escape",
        "database": {"completed": True, "statements": 12, "private": "must-not-escape"},
        "operations": [{"operation": "directory_fetch", "calls": 1, "private": "must-not-escape", "failures": []}],
    }


def _arguments(tmp_path: Path) -> tuple[list[str], Path]:
    configuration = tmp_path / "runtime.json"
    configuration.write_text("{}", encoding="utf-8")
    output = tmp_path / "measurement.json"
    return ["--runtime-config", str(configuration), "--measurement-output", str(output)], output


def test_help_does_not_import_the_measured_sync_or_product_configuration() -> None:
    source = (
        "import sys\nfrom scripts.download.company.live_sync import Run\n"
        "try:\n Run('company-live-sync',['--help'])\n"
        "except SystemExit as error:\n assert error.code == 0\n"
        "else:\n raise AssertionError('help did not exit')\n"
        "assert 'scripts.download.company.company_pipeline_audit' not in sys.modules\n"
        "assert 'trader.bootstrap' not in sys.modules\n"
    )
    completed = subprocess.run((sys.executable, "-c", source), cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    assert "WRITES CURRENT COMPANY DATA" in completed.stdout


def test_success_uses_the_existing_measurement_and_redirects_product_stdout(tmp_path, monkeypatch, capsys) -> None:
    from scripts.download.company import company_pipeline_audit

    arguments, output = _arguments(tmp_path)
    calls = []

    def measured(configuration, destination, *, fill_missing):
        calls.append((configuration, destination, fill_missing))
        print("product progress")
        destination.write_text(json.dumps(_measurement()), encoding="utf-8")
        return 0

    monkeypatch.setattr(company_pipeline_audit, "MeasureCompanySync", measured)
    outcome = live_sync.Run("company-live-sync", arguments + ["--fill"])
    assert outcome.exit_code == 0 and outcome.payload["status"] == "passed"
    assert outcome.payload["measurement_status"] == "passed"
    assert outcome.payload["field_correctness_verified"] is False
    assert calls == [(tmp_path / "runtime.json", output, True)]
    assert "must-not-escape" not in json.dumps(outcome.payload)
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "product progress\n"


@pytest.mark.parametrize("code", (1, 2, 7, 130))
def test_nonzero_cli_exit_is_preserved_without_promoting_a_degraded_measurement(tmp_path, monkeypatch, code) -> None:
    from scripts.download.company import company_pipeline_audit

    arguments, _output = _arguments(tmp_path)

    def measured(_configuration, destination, *, fill_missing):
        destination.write_text(json.dumps(_measurement(code, status="degraded", cancelled=code == 130)), encoding="utf-8")
        return code

    monkeypatch.setattr(company_pipeline_audit, "MeasureCompanySync", measured)
    outcome = live_sync.Run("company-live-sync", arguments)
    assert outcome.exit_code == code
    assert outcome.payload["cli_exit_code"] == code
    assert outcome.payload["status"] == ("cancelled" if code == 130 else "failed")
    assert outcome.payload["measurement_status"] == "degraded"


def test_keyboard_interrupt_keeps_the_measurement_unknown_cli_exit(tmp_path, monkeypatch) -> None:
    from scripts.download.company import company_pipeline_audit

    arguments, _output = _arguments(tmp_path)

    def measured(_configuration, destination, *, fill_missing):
        destination.write_text(json.dumps(_measurement(None, status="degraded", cancelled=True)), encoding="utf-8")
        raise KeyboardInterrupt

    monkeypatch.setattr(company_pipeline_audit, "MeasureCompanySync", measured)
    outcome = live_sync.Run("company-live-sync", arguments)
    assert outcome.exit_code == 130 and outcome.payload["status"] == "cancelled"
    assert outcome.payload["cli_exit_code"] is None


@pytest.mark.parametrize("fault", ("missing", "too_large", "running", "wrong_exit", "claimed_quality"))
def test_unusable_measurement_cannot_turn_a_zero_return_into_acceptance(tmp_path, monkeypatch, fault) -> None:
    from scripts.download.company import company_pipeline_audit

    arguments, _output = _arguments(tmp_path)

    def measured(_configuration, destination, *, fill_missing):
        if fault == "missing":
            return 0
        if fault == "too_large":
            destination.write_bytes(b" " * (live_sync._MAX_REPORT_BYTES + 1))
            return 0
        payload = _measurement()
        if fault == "running":
            payload["status"] = "running"
            payload["database_statistics_final"] = False
        elif fault == "wrong_exit":
            payload["cli_exit_code"] = 1
        else:
            payload["field_correctness_verified"] = True
        destination.write_text(json.dumps(payload), encoding="utf-8")
        return 0

    monkeypatch.setattr(company_pipeline_audit, "MeasureCompanySync", measured)
    outcome = live_sync.Run("company-live-sync", arguments)
    assert outcome.exit_code == 1 and outcome.payload["status"] == "failed"


def test_existing_measurement_rejects_before_the_sync_is_imported_or_called(tmp_path, monkeypatch) -> None:
    arguments, output = _arguments(tmp_path)
    output.write_text("retained", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        live_sync.Run("company-live-sync", arguments)
    assert error.value.code == 2
    assert output.read_text(encoding="utf-8") == "retained"


def test_public_route_keeps_one_json_document_and_the_measurement_evidence(tmp_path, monkeypatch, capsys) -> None:
    from scripts.core.cli import Main
    from scripts.download.company import company_pipeline_audit

    arguments, _output = _arguments(tmp_path)

    def measured(_configuration, destination, *, fill_missing):
        print("product progress")
        destination.write_text(json.dumps(_measurement()), encoding="utf-8")
        return 0

    monkeypatch.setattr(company_pipeline_audit, "MeasureCompanySync", measured)
    code = Main(["--business", "download", "--module", "company", "--action", "live-sync", "--", *arguments])
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert code == 0 and report["schema_version"] == "trader-tools"
    assert report["selection"] == {"business": "download", "module": "company", "action": "live-sync"}
    measurement = report["checks"][0]["summary"]
    assert measurement["schema_version"] == "company-live-sync-measurement"
    assert measurement["cli_exit_code"] == 0 and measurement["field_correctness_verified"] is False
    assert captured.err == "product progress\n"


def test_public_report_collision_is_rejected_before_dispatch(tmp_path, monkeypatch) -> None:
    from scripts.core.cli import Main
    from scripts.download.company import company_pipeline_audit

    arguments, output = _arguments(tmp_path)
    monkeypatch.setattr(company_pipeline_audit, "MeasureCompanySync", lambda *args, **kwargs: pytest.fail("sync dispatched"))
    with pytest.raises(SystemExit) as error:
        Main(["--business", "download", "--module", "company", "--action", "live-sync", "--report", str(output), "--", *arguments])
    assert error.value.code == 2
    assert not output.exists()
