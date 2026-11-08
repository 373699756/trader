"""Train output contracts, exclusive diagnostics and safe exception evidence."""

import errno
import io
import json
from pathlib import Path

import pytest

from trader.entrypoints.cli import Main
from trader.training.application.error_diagnostics import CaptureTrainingError
from trader.training.entrypoints.report_output import ProjectTrainingError, TrainingReportOutput


def test_default_is_quiet_and_diagnostic_file_keeps_complete_dates(tmp_path, capsys):
    path = tmp_path / "diagnostics.json"
    payload = {"date_coverage": {"required_dates": ["2026-11-05", "2026-11-06"]}}
    with TrainingReportOutput(diagnostics=path) as output:
        output.emit(payload)
        output.emit({"status": "completed"})
    assert capsys.readouterr().out == ""
    assert json.loads(path.read_text()) == {"reports": [payload, {"status": "completed"}]}


def test_json_is_explicit_and_file_creation_never_overwrites(tmp_path, capsys):
    path = tmp_path / "existing.json"
    path.write_text("keep")
    with pytest.raises(FileExistsError), TrainingReportOutput(diagnostics=path):
        pytest.fail("existing file accepted")
    assert path.read_text() == "keep"
    link = tmp_path / "link.json"
    link.symlink_to(tmp_path / "missing.json")
    with pytest.raises(FileExistsError), TrainingReportOutput(diagnostics=link):
        pytest.fail("symlink accepted")
    with TrainingReportOutput(json_output=True) as output:
        output.emit({"status": "failed"})
    assert json.loads(capsys.readouterr().out) == {"status": "failed"}


@pytest.mark.parametrize("prefix", ("/private/checkout/src", "/private/site-packages"))
def test_diagnostics_keep_chain_and_product_locations_without_sensitive_payloads(prefix):
    # A synthetic product filename verifies traceback projection without executing training.
    namespace = {}
    exec(compile(
        "def fail():\n    try:\n        raise OSError(28, '/private/path token=secret')\n"
        "    except OSError as exc:\n        raise RuntimeError('training_fit_failed') from exc\n",
        prefix + "/trader/training/infra/example.py", "exec",
    ), namespace)
    try:
        namespace["fail"]()
    except RuntimeError as exc:
        diagnostic = CaptureTrainingError(exc)
    projected = ProjectTrainingError(diagnostic)
    assert [cause.error_type for cause in diagnostic.causes] == ["RuntimeError", "OSError"]
    assert diagnostic.causes[1].errno == errno.ENOSPC
    assert diagnostic.causes[0].frames[-1].source == "trader/training/infra/example.py"
    assert diagnostic.causes[0].frames[-1].function == "fail"
    assert diagnostic.causes[0].frames[-1].line == 5
    assert all(value not in str(projected) for value in ("private", "secret", "token", "checkout"))


@pytest.mark.parametrize("terminal", ("success", "failure", "cancelled"))
def test_cli_default_progress_and_complete_diagnostics(tmp_path, monkeypatch, capsys, terminal):
    from tests.helpers.shared.cold_start import _empty_config

    path = tmp_path / "diagnostics.json"

    def train(_runtime, _profiles, **options):
        if terminal == "failure":
            raise RuntimeError("training_fixture_failed")
        if terminal == "cancelled":
            raise KeyboardInterrupt
        options["report_output"].emit({"status": "completed", "available_dates": ["2026-11-06"]})
        return 0

    monkeypatch.setattr("trader.bootstrap.ExecuteSelectedTraining", train)
    code = Main(["--config", str(_empty_config(tmp_path)), "train", "v1", "--diagnostics", str(path)])
    assert code == {"success": 0, "failure": 1, "cancelled": 130}[terminal]
    captured = capsys.readouterr()
    assert captured.out == "" and "train" in captured.err
    reports = json.loads(path.read_text())["reports"]
    assert reports[-1]["status"] == {"success": "completed", "failure": "failed", "cancelled": "cancelled"}[terminal]
    if terminal == "failure":
        assert "training_fixture_failed" in captured.err
        assert reports[-1]["diagnostic"]["causes"][0]["error_type"] == "RuntimeError"


def test_conflicting_diagnostics_reject_before_loading_config(tmp_path, monkeypatch, capsys):
    path = tmp_path / "existing.json"
    path.write_text("keep")
    monkeypatch.setattr("trader.entrypoints.cli.LoadRuntimeSettings", lambda *_: pytest.fail("configuration loaded"))
    assert Main(["train", "--diagnostics", str(path)]) == 1
    assert path.read_text() == "keep"
    assert "FileExistsError" in capsys.readouterr().err


def test_diagnostics_write_failure_is_visible(tmp_path, monkeypatch, capsys):
    def write_error(_path, *_args, **_kwargs):
        raise OSError(errno.ENOSPC, "private path")

    monkeypatch.setattr(Path, "open", write_error)
    assert Main(["train", "--diagnostics", str(tmp_path / "report.json")]) == 1
    captured = capsys.readouterr()
    assert "errno=28" in captured.err and "private path" not in captured.err


def test_result_write_failure_never_reports_command_success(tmp_path, monkeypatch, capsys):
    from tests.helpers.shared.cold_start import _empty_config

    config = _empty_config(tmp_path)
    path = tmp_path / "report.json"
    original_open = Path.open

    class FullDisk(io.StringIO):
        def write(self, text):
            if '"status"' in text:
                raise OSError(errno.ENOSPC, "private path")
            return super().write(text)

    monkeypatch.setattr(Path, "open", lambda candidate, *args, **kwargs:
                        FullDisk() if candidate == path else original_open(candidate, *args, **kwargs))

    def train(_runtime, _profiles, **options):
        options["report_output"].emit({"status": "completed"})
        return 0

    monkeypatch.setattr("trader.bootstrap.ExecuteSelectedTraining", train)
    assert Main(["--config", str(config), "train", "--diagnostics", str(path)]) == 1
    captured = capsys.readouterr()
    assert "errno=28" in captured.err and "private path" not in captured.err
