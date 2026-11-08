"""Source-mode configuration identity is validated before the closed HTTP boundary."""

import argparse
import hashlib
import json
import shutil
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from scripts.core.paths import PROJECT_ROOT
from scripts.download.company import research_source_compare as worker


@pytest.mark.parametrize("arguments", (
    ("--st-name-history",),
    ("--st-name-history", "--codes", "300101", "300101"),
    ("--st-name-history", "--codes", "920001"),
    ("--st-name-history", "--codes", "300101", "--st-rounds", "6"),
    ("--st-name-history", "--codes", "300101", "--timeout-seconds", "16"),
    ("--st-workers", "3"),
))
def test_st_worker_rejects_invalid_selection_before_reading_configuration(monkeypatch, arguments):
    monkeypatch.setattr(worker, "LoadRuntimeSettings", lambda *args: pytest.fail("configuration must remain unopened"))
    monkeypatch.setattr(worker, "OpenExternalHttp", lambda: pytest.fail("HTTP must remain closed"))
    monkeypatch.setattr("sys.argv", ["research-source-compare", "--config", "unused.json", *arguments])
    with pytest.raises(SystemExit):
        worker.Main()


def test_st_worker_binds_one_readonly_reference_and_cannot_create_missing_codes(tmp_path, monkeypatch):
    reference = SimpleNamespace(
        official_universe=(SimpleNamespace(code="600848", name="上海临港"),),
        st_evidence=(SimpleNamespace(code="600848", status="ever_st"),), identity="a" * 64,
    )
    reads = []
    selected = SimpleNamespace(data_paths=SimpleNamespace(reference_database=tmp_path / "reference.sqlite3"))
    policy = object()
    monkeypatch.setattr(worker, "BuildSqlitePolicy", lambda runtime: policy if runtime is selected else pytest.fail("wrong runtime"))

    class Index:
        def __init__(self, path, *, sqlite_policy):
            assert path == selected.data_paths.reference_database and sqlite_policy is policy

        def read(self):
            reads.append(True)
            return reference

    monkeypatch.setattr(worker, "SQLiteReferenceIndex", Index)
    requests, identity = worker._st_requests(selected, ("600848",))
    assert len(requests) == 1 and requests[0].expected_status == "ever_st" and identity == "a" * 64
    with pytest.raises(ValueError, match="reference_code_missing"):
        worker._st_requests(selected, ("300101",))
    assert len(reads) == 2


def test_st_worker_selects_only_readonly_audit_and_keeps_reference_identity(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(worker, "_st_requests", lambda *_args: (("typed_requests",), "a" * 64))
    monkeypatch.setattr(worker, "AuditCompanyStSources", lambda requests, **kwargs: calls.append((requests, kwargs)) or {
        "probe": "st_name_history", "status": "degraded", "read_only": True,
    })
    monkeypatch.setattr(worker, "Collect", lambda *_args, **_kwargs: pytest.fail("market-wide sources must remain closed"))
    monkeypatch.setattr("sys.argv", ["research-source-compare", "--config", str(PROJECT_ROOT / "config/runtime.json"),
                                     "--st-name-history", "--codes", "300101", "600848", "--st-rounds", "2", "--st-workers", "1"])
    assert worker.Main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["reference_hash"] == "a" * 64 and len(calls) == 1
    assert calls[0][1]["rounds"] == 2 and calls[0][1]["workers"] == 1


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
