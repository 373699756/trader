"""Publication coverage and receipt diagnostics must not turn missing evidence into success."""

from tests.helpers.configuration import SqlitePolicyFixture

import json
import time
from dataclasses import replace
from datetime import timedelta

import pytest

from scripts.diagnose_runtime import DiagnosticResult, build_report
from scripts.runtime_diagnostics import company_publication_audit
from scripts.runtime_diagnostics.company_publication_audit import AuditCompanyPublication
from scripts.runtime_diagnostics.company_query_audit import AuditCompanyDatabase
from tests.component import test_company_evidence as fixtures
from trader.download.domain.company_evidence import DisclosurePage
from trader.download.domain.download_failure import DownloadFailureConfiguration, DownloadFailureContext, DownloadFailureRecord
from trader.download.infra.company_raw_files import CompanyRawFiles
from trader.download.infra.company_facts_protocol import EncodeCompanyDate
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction


def _published(tmp_path):
    index = fixtures._index(tmp_path)
    partition = index.partitions(("sse",), fixtures.NOW)[0]
    item = fixtures._disclosure(title="关于收到立案告知书的公告")
    index.commit_page(partition, DisclosurePage((item,), 1, False), fixtures.NOW)
    index.commit_document(item, fixtures._extraction("本公司收到证监会立案告知书。"), fixtures.NOW)
    index.publish(("600001",), fixtures.NOW)
    return index


def _audit(index, now=fixtures.NOW):
    return AuditCompanyPublication(index.path, index.facts_path, now=now, deadline=time.monotonic() + 10, sqlite_policy=SqlitePolicyFixture())


def test_published_projection_keeps_unknown_components_and_valid_released_receipts_distinct(tmp_path):
    index = _published(tmp_path)
    before = (index.path.read_bytes(), index.facts_path.read_bytes())
    result = _audit(index)
    assert result["stable_read"] and result["population_stocks"] == result["published_stocks"] == 1
    assert result["receipts"] == {
        "checked": True, "awaiting_publication_documents": 0, "released_without_receipts": 0,
    }
    assert result["ongoing_risks"]["events"] == 1
    assert result["coverage"]["unlock_incomplete_stocks"] == 1
    assert "company_components_incomplete" in result["findings"]
    assert "company_reviews_pending" in result["findings"]
    assert (index.path.read_bytes(), index.facts_path.read_bytes()) == before
    audit = result["field_audit"]
    assert audit["table_count"] == 13 and audit["field_count"] == 106
    assert audit["invalid_value_types"] == 0 and audit["source_agreement"]["unlocated_evidence_refs"] == 0


def test_field_audit_distinguishes_real_source_values_from_formal_corruption(tmp_path):
    index = _published(tmp_path)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("INSERT INTO financials VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("600001", str(fixtures.NOW.date()), fixtures.NOW.isoformat(), 1, 2, 3, 4, 5, 6, 7, 8))
    index.publish(("600001",), fixtures.NOW)
    before = _audit(index)["field_audit"]
    assert before["source_agreement"]["financial_rows"] == 1
    assert before["source_agreement"]["financial_value_mismatches"] == 0
    with SqliteSession(index.facts_path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE company_facts SET eps='invalid' WHERE code='600001'")
    after = _audit(index)["field_audit"]
    assert after["invalid_value_types"] == 1
    assert after["source_agreement"]["financial_value_mismatches"] == 1
    with SqliteSession(index.facts_path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE company_facts SET eps=? WHERE code='600001'", (float("inf"),))
    assert _audit(index)["field_audit"]["nonfinite_values"] == 1


@pytest.mark.parametrize("change", ("delete", "record_hash", "extraction_hash", "new_member"))
def test_released_body_requires_current_version_receipt_for_every_population_member(tmp_path, change):
    index = _published(tmp_path)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        if change == "new_member":
            connection.run("INSERT INTO company_population VALUES('600002')")
            connection.run("INSERT INTO disclosure_codes(code,disclosure_id) SELECT '600002',id FROM disclosures")
    with SqliteSession(index.facts_path) as connection, SqliteTransaction(connection):
        if change == "delete":
            connection.run("DELETE FROM company_input_receipt")
        elif change == "record_hash":
            connection.run("UPDATE company_input_receipt SET record_hash='old'")
        elif change == "extraction_hash":
            connection.run("UPDATE company_input_receipt SET extraction_hash=?", (b"x" * 32,))
    result = _audit(index)
    assert result["receipts"]["released_without_receipts"] == 1
    assert "company_released_body_receipt_missing" in result["findings"]
    if change == "new_member":
        assert result["unpublished_stocks"] == 1


def test_collection_receipt_loss_is_recoverable_from_formal_success(tmp_path):
    index = _published(tmp_path)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("DELETE FROM document_publications")
    result = _audit(index)
    assert result["receipts"]["released_without_receipts"] == 0
    assert "company_released_body_receipt_missing" not in result["findings"]


def test_retained_unpublished_document_is_pending_and_not_lost(tmp_path):
    index = _published(tmp_path)
    partition = index.partitions(("sse",), fixtures.NOW)[1]
    item = replace(fixtures._disclosure("next", "关于收到立案告知书的公告"), published_at=fixtures.NOW)
    index.commit_page(partition, DisclosurePage((item,), 1, False), fixtures.NOW)
    index.commit_document(item, fixtures._extraction("本公司收到证监会立案告知书。"), fixtures.NOW)
    result = _audit(index)
    assert result["receipts"]["awaiting_publication_documents"] == 1
    assert result["receipts"]["released_without_receipts"] == 0
    assert result["dirty_stocks"] == 1
    assert "company_publication_dirty" in result["findings"]


def test_unlock_horizon_ages_and_long_lived_risks_remain_visible(tmp_path):
    index = _published(tmp_path)
    with SqliteSession(index.facts_path) as connection, SqliteTransaction(connection):
        connection.run(
            "UPDATE company_facts SET unlock_state=2,unlock_end_date=?",
            (EncodeCompanyDate(fixtures.NOW.date() + timedelta(days=180)),),
        )
    assert _audit(index)["coverage"]["unlock_incomplete_stocks"] == 0
    assert _audit(index, fixtures.NOW + timedelta(days=1))["coverage"]["unlock_incomplete_stocks"] == 1
    assert _audit(index, fixtures.NOW + timedelta(days=366))["ongoing_risks"]["older_than_year"] == 1


def test_dirty_codes_outside_population_are_not_reported_as_unpublished_current_stocks(tmp_path):
    index = _published(tmp_path)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("INSERT OR IGNORE INTO company_dirty_codes VALUES('900901')")
    result = _audit(index)
    assert result["dirty_stocks"] == 0
    assert result["outside_population_dirty_stocks"] == 1
    assert "company_publication_dirty" not in result["findings"]
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("INSERT OR IGNORE INTO company_population VALUES('900901')")
    result = _audit(index)
    assert result["dirty_stocks"] == 1
    assert result["outside_population_dirty_stocks"] == 0
    assert "company_publication_dirty" in result["findings"]


def test_empty_collection_without_formal_publication_cannot_pass(tmp_path):
    index = fixtures._index(tmp_path)
    result = AuditCompanyDatabase(index.path, facts_path=index.facts_path, now=fixtures.NOW, budget_seconds=10, sqlite_policy=SqlitePolicyFixture())
    assert result["status"] == "degraded"
    assert "company_publication_missing" in result["findings"]
    assert "company_population_empty" in result["findings"]


def test_disk_inventory_includes_history_beyond_summary_limit_without_touching_inputs(tmp_path):
    index = _published(tmp_path)
    item = fixtures._disclosure(title="关于收到立案告知书的公告")
    index.document_failure(item, "old-policy", "FileNotFoundError", fixtures.NOW, fixtures.NOW)
    raw = CompanyRawFiles(tmp_path, capacity=1000000, analysis_reserve=100000, free_reserve=1000)
    directory = raw.directory(item)
    directory.mkdir(parents=True)
    invalid = directory / "invalid.pdf"
    invalid.write_bytes(b"not-a-valid-pdf-private-content")
    configuration = DownloadFailureConfiguration(20000, 50)
    for _ in range(55):
        index.record_failure(DownloadFailureRecord(
            "test-run", fixtures.NOW, DownloadFailureContext("company_document", code="600001", supplier="cninfo"),
            "FileNotFoundError", "FileNotFoundError:errno_2",
            "FileNotFoundError:pathlib.py:_scandir:938 > stage=download;record=first;version=a",
        ), configuration=configuration)
    before = (index.path.read_bytes(), index.facts_path.read_bytes(), invalid.read_bytes())
    result = AuditCompanyDatabase(
        index.path, facts_path=index.facts_path, now=fixtures.NOW, budget_seconds=10, sqlite_policy=SqlitePolicyFixture(),
    )
    inventory = result["summary"]["database"]
    assert len(inventory["failure_history"]["events"]) == inventory["failure_history"]["total"] == 55
    assert inventory["retry_inventory"][0]["files"] == ["invalid.pdf"]
    assert inventory["retry_inventory"][0]["pdf_files"] == 1
    assert inventory["failure_receipts"][0]["outcome"] == "valid"
    assert not inventory["file_inventory_is_atomic"]
    report = build_report("company-resume", [DiagnosticResult("company_resume", 0, 1.0, result, None)])
    assert "not-a-valid-pdf-private-content" not in json.dumps(report)
    assert len(report["checks"][0]["summary"]["database"]["retry_inventory"]) == 1
    assert (index.path.read_bytes(), index.facts_path.read_bytes(), invalid.read_bytes()) == before


def test_audit_detects_concurrent_commit_instead_of_claiming_consistent_output(tmp_path, monkeypatch):
    index = _published(tmp_path)
    read_identity = company_publication_audit.ReadCompanyPublicationIdentity

    def read_and_change(connection):
        identity = read_identity(connection)
        with SqliteSession(index.path) as writer, SqliteTransaction(writer):
            writer.run("INSERT OR IGNORE INTO company_dirty_codes VALUES('600001')")
        return identity

    monkeypatch.setattr(company_publication_audit, "ReadCompanyPublicationIdentity", read_and_change)
    result = _audit(index)
    assert not result["stable_read"]
    assert "company_audit_concurrent_change" in result["findings"]


def test_public_report_projects_only_aggregate_coverage_fields(tmp_path):
    index = _published(tmp_path)
    publication = _audit(index)
    publication["private_codes"] = ["must-not-project"]
    publication["reviews"][0]["body"] = "must-not-project"
    publication["receipts"]["path"] = "must-not-project"
    publication["findings"].extend(["must-not-project", {"body": "must-not-project"}])
    report = build_report("company-resume", [DiagnosticResult(
        "company_resume", 0, 1.0, {"schema_version": "company-resume", "status": "degraded",
                                 "summary": {"database": {"publication": publication}}}, None,
    )])
    assert "must-not-project" not in json.dumps(report)
    assert "company_components_incomplete" in json.dumps(report)
    fields = report["checks"][0]["summary"]["database"]["publication"]["field_audit"]
    assert fields["invalid_value_types"] == publication["field_audit"]["invalid_value_types"]
