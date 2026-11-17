"""Formal reader consistency is independent of source quality and repair planning."""

import json
import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest

from scripts.core.contracts import DiagnosticResult
from scripts.core.projections import BuildReport
from scripts.download.company import company_publication_audit as audit
from scripts.download.company import company_resume
from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.shared.tool_cli import BuildToolCommands, ParseBusinessOptions, ParseToolArguments
from trader.download.domain.company_document_links import CompanyPublishedEvidenceLink
from trader.download.domain.company_events import CompanyEvidenceLink


def _index(tmp_path):
    index = fixtures._index(tmp_path)
    index.publish(("600001",), fixtures.NOW)
    return index


def _audit(index):
    return audit.AuditCompanyConsumer(
        index.facts_path, now=fixtures.NOW, budget_seconds=30,
        sqlite_policy=SqlitePolicyFixture(), event_policy=fixtures.CompanyEventPolicy(),
    )


def test_formal_consumer_audit_never_plans_repairs_or_checks_source_quality(tmp_path, monkeypatch):
    index = _index(tmp_path)
    before = index.facts_path.read_bytes(), index.path.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("consumer consistency must not inspect collection recovery")

    monkeypatch.setattr(audit, "AuditCompanyFields", forbidden)
    monkeypatch.setattr(audit, "ReadUnusedCompanyFinancialInputs", forbidden)
    report = _audit(index)
    summary = report["summary"]["consumer_audit"]
    assert report["status"] == "passed" and summary["complete"] and summary["stable_read"]
    assert summary["consumer_read"]["stocks"] == 1 and summary["consumer_read"]["consistent"]
    assert summary["scope"] == "formal_reader_consistency_only"
    assert all(summary[name] is False for name in (
        "collection_checked", "recovery_planned", "field_correctness_verified", "full_market_verified", "http_checked",
    ))
    assert before == (index.facts_path.read_bytes(), index.path.read_bytes())


def test_concurrent_formal_writer_prevents_a_consistency_pass(tmp_path, monkeypatch):
    index = _index(tmp_path)
    visit = audit.SQLiteCompanyEvidenceReader.visit

    def changed(reader, *args, **kwargs):
        result = visit(reader, *args, **kwargs)
        index.publish(("600001",), fixtures.NOW + timedelta(days=1))
        return result

    monkeypatch.setattr(audit.SQLiteCompanyEvidenceReader, "visit", changed)
    report = _audit(index)
    assert report["status"] == "degraded"
    assert not report["summary"]["consumer_audit"]["complete"]
    assert "company_audit_concurrent_change" in report["findings"]


def test_reader_link_mismatch_cannot_pass_fact_and_field_consistency(tmp_path, monkeypatch):
    index = _index(tmp_path)
    read = audit.SQLiteCompanyEvidenceReader.read_document_links

    def different(reader, codes, **kwargs):
        snapshot = read(reader, codes, **kwargs)
        return replace(snapshot, links=(CompanyPublishedEvidenceLink(
            "600001", "cninfo", "a" * 64,
            CompanyEvidenceLink("unexpected", "额外链接", "https://example.test", fixtures.NOW, False),
        ),))

    monkeypatch.setattr(audit.SQLiteCompanyEvidenceReader, "read_document_links", different)
    report = _audit(index)
    consumer = report["summary"]["consumer_audit"]["consumer_read"]
    assert consumer["facts_equal"] and consumer["fields_equal"] and consumer["publication_equal"]
    assert not consumer["document_links_equal"] and not consumer["consistent"]
    assert report["status"] == "degraded"


@pytest.mark.parametrize("known", (True, False))
def test_only_own_reader_cancellation_becomes_a_budget_failure(tmp_path, monkeypatch, known):
    index = _index(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(audit.time, "monotonic", lambda: clock[0])
    error = InterruptedError("company_preload_cancelled" if known else "unrelated_interruption")

    def interrupted(*args, **kwargs):
        clock[0] = 30
        assert kwargs["cancel_requested"]()
        raise error

    monkeypatch.setattr(audit.SQLiteCompanyEvidenceReader, "visit", interrupted)
    if not known:
        with pytest.raises(InterruptedError) as captured:
            _audit(index)
        assert captured.value is error
        return
    report = _audit(index)
    summary = report["summary"]["consumer_audit"]
    assert report["status"] == "failed" and summary["budget_exhausted"]
    assert not summary["complete"] and summary["consumer_read"] == {"checked": False, "http_checked": False}


def test_missing_formal_database_is_not_created(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        audit.AuditCompanyConsumer(
            missing, now=fixtures.NOW, budget_seconds=30,
            sqlite_policy=SqlitePolicyFixture(), event_policy=fixtures.CompanyEventPolicy(),
        )
    assert not missing.exists()


def test_end_deadline_does_not_erase_an_already_observed_reader_failure(tmp_path, monkeypatch):
    index = _index(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(audit.time, "monotonic", lambda: clock[0])

    def unreadable(*args, **kwargs):
        clock[0] = 30
        raise OSError("private reader failure")

    monkeypatch.setattr(audit.SQLiteCompanyEvidenceReader, "visit", unreadable)
    report = _audit(index)
    summary = report["summary"]["consumer_audit"]
    assert report["status"] == "failed" and summary["budget_exhausted"]
    assert summary["consumer_read"]["failure"] == "company_consumer_unreadable"
    assert "company_consumer_read_not_verified" in report["findings"]
    assert "private reader failure" not in json.dumps(report)


def test_public_consumer_mode_is_read_only_and_does_not_require_replay(tmp_path):
    arguments = [
        "--business", "download", "--module", "company", "--action", "coverage",
        "--company-consumer-audit", "--company-evidence-database", str(tmp_path / "facts.sqlite3"),
    ]
    options = ParseBusinessOptions(ParseToolArguments(arguments))
    command, = BuildToolCommands(options)
    assert "--consumer-only" in command.argv and "--directory" not in command.argv
    with pytest.raises(SystemExit):
        ParseToolArguments([*arguments, "--company-database-audit"])
    with pytest.raises(ValueError, match="replay directory"):
        ParseBusinessOptions(ParseToolArguments([*arguments, "--company-replay-directory", str(tmp_path / "replay")]))


def test_worker_consumer_mode_does_not_resolve_a_collection_database(tmp_path, monkeypatch, capsys):
    index = _index(tmp_path)
    monkeypatch.setattr("sys.argv", [
        "company_resume", "--consumer-only", "--facts-database", str(index.facts_path), "--budget-seconds", "30",
    ])
    assert company_resume.Main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "passed" and report["summary"]["consumer_audit"]["consumer_read"]["consistent"]


def test_http_modifier_requires_consumer_mode_and_reaches_worker(tmp_path):
    arguments = ["--business", "download", "--module", "company", "--action", "coverage", "--company-consumer-http"]
    with pytest.raises(ValueError, match="requires --company-consumer-audit"):
        ParseBusinessOptions(ParseToolArguments(arguments))
    options = ParseBusinessOptions(ParseToolArguments([
        *arguments, "--company-consumer-audit", "--company-evidence-database", str(tmp_path / "facts.sqlite3"),
        "--company-collection-database", str(tmp_path / "collection.sqlite3"),
    ]))
    command, = BuildToolCommands(options)
    assert "--consumer-only" in command.argv and "--consumer-http" in command.argv


def _http_evidence(summary):
    digest = "a" * 64
    return {
        "scope": "published_company_document_http", "passed": True, "complete": True,
        "expected_publication_hash": summary["publication_hash"], "loaded_identity": summary["publication_hash"],
        "codes": summary["consumer_read"]["stocks"], "verified_links": 1, "http_requests": 6,
        "invalid_queries_rejected": 5, "links_digest": digest,
        **{key: 0 for key in ("handler_file_attempts", "handler_sql_attempts", "handler_network_attempts",
                             "external_network_attempts", "business_write_attempts")},
        "source_unchanged": True, "source_payload_sha256_before": [digest, None, digest],
        "source_payload_sha256_after": [digest, None, digest], "server_stopped": True, "resources_stopped": True,
        "budget_exhausted": False, "failure": None, "source_admission_verified": False,
        "production_enabled": False, "full_sync_verified": False, "performance_accepted": False,
    }


@pytest.mark.parametrize("change", (None, "identity", "io", "links", "shutdown", "quality", "fingerprint"))
def test_http_public_evidence_requires_matching_identity_no_io_and_bounded_shutdown(tmp_path, change):
    report = _audit(_index(tmp_path))
    summary = report["summary"]["consumer_audit"]
    summary.update(scope="formal_reader_and_document_http", http_checked=True)
    summary["consumer_read"]["document_links"] = 1
    http = _http_evidence(summary)
    summary["http_audit"] = http
    http["private_payload"] = "DO_NOT_EXPORT"
    if change == "identity":
        http["loaded_identity"] = "b" * 64
    elif change == "io":
        http["handler_sql_attempts"] = 1
    elif change == "links":
        http["verified_links"] = 0
    elif change == "shutdown":
        http["resources_stopped"] = False
    elif change == "quality":
        http["source_admission_verified"] = True
    elif change == "fingerprint":
        http["source_payload_sha256_after"] = ["b" * 64, None, "a" * 64]
    projected = BuildReport("company-resume", (DiagnosticResult("company_resume", 0, 0.1, report, None),))
    assert projected["status"] == ("passed" if change is None else "failed")
    assert "DO_NOT_EXPORT" not in json.dumps(projected)


@pytest.mark.parametrize("change", ("scope", "quality", "stable", "stocks", "identity", "consistency"))
def test_public_projection_rejects_contradictory_passes_and_hides_extra_data(tmp_path, change):
    report = _audit(_index(tmp_path))
    summary = report["summary"]["consumer_audit"]
    summary["private_payload"] = "DO_NOT_EXPORT"
    reader = summary["consumer_read"]
    reader["private_payload"] = "DO_NOT_EXPORT"
    valid = BuildReport("company-resume", (DiagnosticResult("company_resume", 0, 0.1, report, None),))
    assert valid["status"] == "passed" and "DO_NOT_EXPORT" not in json.dumps(valid)
    if change == "scope":
        summary["scope"] = "whole_company_quality"
    elif change == "quality":
        summary["field_correctness_verified"] = True
    elif change == "stable":
        summary["stable_read"] = False
    elif change == "stocks":
        reader["stocks"] = True
    elif change == "identity":
        summary["publication_hash"] = "wrong"
    else:
        reader["consistent"] = False
    rejected = BuildReport("company-resume", (DiagnosticResult("company_resume", 0, 0.1, report, None),))
    assert rejected["status"] == "failed"
