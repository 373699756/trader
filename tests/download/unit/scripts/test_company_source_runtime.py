"""Source-mode configuration identity is validated before the closed HTTP boundary."""

import argparse
import hashlib
import json
import shutil
from contextlib import nullcontext

import pytest

from scripts.core.paths import PROJECT_ROOT
from scripts.download.company import research_source_compare as worker


@pytest.mark.parametrize("mode", ("market", "fields", "annual", "revision"))
def test_every_source_mode_validates_selected_runtime_and_reports_its_identity(tmp_path, monkeypatch, capsys, mode):
    selected = tmp_path / "selected-checkout"
    shutil.copytree(PROJECT_ROOT / "config", selected / "config")
    configuration = selected / "config/runtime.json"
    selected_roots = []

    def collect(args, *, project_root):
        selected_roots.append(project_root)
        return {"schema_version": "research-source-comparison", "status": "passed"}

    monkeypatch.setattr(worker, "Collect", collect)
    monkeypatch.setattr(worker, "OpenExternalHttp", lambda: nullcontext(object()))
    monkeypatch.setattr(worker, "AuditCompanyStructuredSources", lambda *args, **kwargs: {"schema_version": "company-structured-field-sampling"})
    monkeypatch.setattr(worker, "AuditCompanyAnnualIncome", lambda *args, **kwargs: {"schema_version": "company-annual-income-source"})
    monkeypatch.setattr(worker, "AuditCompanyFinancialRevision", lambda *args, **kwargs: {"status": "degraded", "financial_revision_probe": {"read_only": True}})
    arguments = ["research-source-compare", "--config", str(configuration)]
    arguments += ["--field-sources-only"] if mode == "fields" else ["--codes", "600001", "--annual-income-period", "2025-12-31"] if mode == "annual" else []
    if mode == "revision":
        arguments += ["--codes", "600001", "--financial-revision-period", "2026-06-30"]
    monkeypatch.setattr("sys.argv", arguments)
    assert worker.Main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["runtime_configuration_sha256"] == hashlib.sha256(configuration.read_bytes()).hexdigest()
    assert selected_roots == ([selected] if mode == "market" else [])


@pytest.mark.parametrize("mode", ((), ("--field-sources-only",), ("--codes", "600001", "--annual-income-period", "2025-12-31"), ("--codes", "600001", "--financial-revision-period", "2026-06-30")))
def test_missing_runtime_cannot_reach_any_http_mode(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(worker, "OpenExternalHttp", lambda: pytest.fail("HTTP must remain closed"))
    monkeypatch.setattr(worker, "Collect", lambda *args, **kwargs: pytest.fail("collection must remain unselected"))
    monkeypatch.setattr("sys.argv", ["research-source-compare", "--config", str(tmp_path / "missing/runtime.json"), *mode])
    with pytest.raises((OSError, ValueError)):
        worker.Main()


def test_market_collection_loads_download_configuration_from_selected_root(tmp_path, monkeypatch):
    class SelectedConfigurationReached(Exception):
        pass

    selected = tmp_path / "selected-checkout"

    def load(project_root):
        assert project_root == selected
        raise SelectedConfigurationReached

    monkeypatch.setattr(worker, "LoadCompanySyncConfiguration", load)
    monkeypatch.setattr(worker, "OpenExternalHttp", lambda: pytest.fail("HTTP must remain closed"))
    with pytest.raises(SelectedConfigurationReached):
        worker.Collect(argparse.Namespace(), project_root=selected)


@pytest.mark.parametrize("mode", ((), ("--field-sources-only",)))
@pytest.mark.parametrize("codes", (("600001",), ("600519", "300750", "688981")))
def test_native_nonannual_modes_reject_any_security_selection_before_config_or_http(tmp_path, monkeypatch, mode, codes):
    monkeypatch.setattr(worker, "LoadRuntimeSettings", lambda *args: pytest.fail("configuration must remain unopened"))
    monkeypatch.setattr(worker, "OpenExternalHttp", lambda: pytest.fail("HTTP must remain closed"))
    monkeypatch.setattr("sys.argv", ["research-source-compare", "--config", str(tmp_path / "runtime.json"), *mode, "--codes", *codes])
    with pytest.raises(SystemExit):
        worker.Main()


@pytest.mark.parametrize("arguments", (
    ("--financial-revision-period", "2026-06-30"),
    ("--codes", "600001", "--financial-revision-period", "2026-06-29"),
    ("--codes", "600001", "000001", "--financial-revision-period", "2026-06-30"),
    ("--codes", "600001", "--financial-revision-period", "2026-06-30", "--field-sources-only"),
    ("--codes", "600001", "--financial-revision-period", "2026-06-30", "--annual-income-period", "2025-12-31"),
))
def test_revision_worker_rejects_invalid_or_combined_modes_before_configuration_and_http(monkeypatch, arguments):
    monkeypatch.setattr(worker, "LoadRuntimeSettings", lambda *args: pytest.fail("configuration must remain unopened"))
    monkeypatch.setattr(worker, "OpenExternalHttp", lambda: pytest.fail("HTTP must remain closed"))
    monkeypatch.setattr("sys.argv", ["research-source-compare", "--config", "unused.json", *arguments])
    with pytest.raises(SystemExit):
        worker.Main()


def test_revision_worker_keeps_a_failed_probe_nonzero_and_does_not_select_market_collection(monkeypatch, capsys):
    monkeypatch.setattr(worker, "OpenExternalHttp", lambda: nullcontext(object()))
    monkeypatch.setattr(worker, "Collect", lambda *args, **kwargs: pytest.fail("market collection must remain unselected"))
    monkeypatch.setattr(worker, "AuditCompanyFinancialRevision", lambda *args, **kwargs: {"status": "failed", "financial_revision_probe": {"request_count": 1}})
    monkeypatch.setattr("sys.argv", ["research-source-compare", "--config", str(PROJECT_ROOT / "config/runtime.json"), "--codes", "600001", "--financial-revision-period", "2026-06-30"])
    assert worker.Main() == 1
    assert json.loads(capsys.readouterr().out)["financial_revision_probe"]["request_count"] == 1
