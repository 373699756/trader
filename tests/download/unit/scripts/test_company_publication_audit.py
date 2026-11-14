"""Publication coverage and receipt diagnostics must not turn missing evidence into success."""

from tests.helpers.shared.configuration import SqlitePolicyFixture

import json
import time
from dataclasses import replace
from datetime import timedelta

import pytest

from scripts.core.contracts import DiagnosticResult
from scripts.core.projections import BuildReport
from scripts.download.company import company_publication_audit
from scripts.download.company.company_publication_audit import AuditCompanyPublication
from scripts.download.company.company_query_audit import AuditCompanyDatabase
from tests.helpers.download.company import company_evidence as fixtures
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
    assert audit["table_count"] == 16 and audit["field_count"] == 137
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


def test_latest_financial_source_nulls_are_separate_from_retained_complete_and_publication_loss(tmp_path):
    from datetime import timedelta

    from scripts.download.company.company_field_audit import ProjectCompanyFieldAudit

    index = _published(tmp_path)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("INSERT INTO financials VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            "600001", str(fixtures.NOW.date() - timedelta(days=90)), fixtures.NOW.isoformat(),
            1, 2, 3, 4, 5, 6, 7, 8,
        ))
        connection.run("INSERT INTO financials VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            "600001", str(fixtures.NOW.date()), fixtures.NOW.isoformat(),
            None, 0, 3, 4, 5, 6, -7, 8,
        ))
    index.publish(("600001",), fixtures.NOW)
    audit = _audit(index)["field_audit"]
    sources = audit["financial_sources"]
    assert sources["retained"]["incomplete_rows"] == 0
    assert sources["latest"]["incomplete_rows"] == 1
    assert sources["latest"]["missing_fields"] == {"eps": 1}
    assert sources["latest"]["reasons"] == {"source_null": 1}
    assert sources["latest"]["value_mismatch_rows"] == 0
    assert "company_latest_financial_fields_missing" in audit["findings"]
    assert ProjectCompanyFieldAudit(audit)["financial_sources"] == sources

    with SqliteSession(index.facts_path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE company_latest_financial SET bps=NULL")
    lost = _audit(index)["field_audit"]
    assert lost["financial_sources"]["latest"]["reasons"] == {"source_null": 1, "publication_missing": 1}
    assert "company_field_integrity_failed" in lost["findings"]


def test_financial_equal_values_with_different_disclosure_identity_are_unconfirmed(tmp_path):
    from datetime import timedelta

    index = _published(tmp_path)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("INSERT INTO financials VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            "600001", str(fixtures.NOW.date()), fixtures.NOW.isoformat(), 1, 2, 3, 4, 5, 6, 7, 8,
        ))
    index.publish(("600001",), fixtures.NOW)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE financials SET published_at=?", ((fixtures.NOW - timedelta(microseconds=1)).isoformat(),))
    audit = _audit(index)["field_audit"]
    latest = audit["financial_sources"]["latest"]
    assert latest["source_identity_unconfirmed_rows"] == 1
    assert latest["reasons"] == {"source_revision_changed": 8}
    assert not latest["value_mismatch_rows"]
    assert "company_financial_source_identity_unconfirmed" in audit["findings"]


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
    raw = CompanyRawFiles(tmp_path, capacity=1000000, analysis_reserve=100000, free_reserve=1000,
                          maximum_document_bytes=64 * 1024**2)
    directory = raw.directory(item)
    directory.mkdir(parents=True)
    invalid = directory / "invalid.pdf"
    invalid.write_bytes(b"not-a-valid-pdf-private-content")
    configuration = DownloadFailureConfiguration(20000, 50)
    for _ in range(55):
        index.record_failure(DownloadFailureRecord(
            "test-run", fixtures.NOW, DownloadFailureContext("company_document", code="600001", supplier="cninfo"),
            "FileNotFoundError", "FileNotFoundError:errno_2",
            "FileNotFoundError:pathlib.py:_scandir:938 > object_id=first > revision=a",
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
    assert inventory["failure_objects"]["states"] == {"published": 1}
    assert inventory["failure_objects"]["objects"][0]["events"] == 55
    assert not inventory["file_inventory_is_atomic"]
    report = BuildReport("company-resume", [DiagnosticResult("company_resume", 0, 1.0, result, None)])
    assert "not-a-valid-pdf-private-content" not in json.dumps(report)
    assert len(report["checks"][0]["summary"]["database"]["retry_inventory"]) == 1
    assert report["checks"][0]["summary"]["database"]["failure_objects"]["states"] == {"published": 1}
    assert (index.path.read_bytes(), index.facts_path.read_bytes(), invalid.read_bytes()) == before


@pytest.mark.parametrize("change,state", [
    ("none", "published"), ("body_checked", "pending_processing"), ("missing_body", "pending_processing"),
    ("body_hash", "pending_publication"), ("extraction_hash", "pending_publication"),
    ("receipt_revision", "pending_publication"), ("receipt_code", "pending_publication"),
    ("outcome", "pending_publication"), ("revision", "revision_changed"),
    ("missing", "source_missing"), ("population", "outside_population"),
])
def test_failure_object_requires_exact_current_input_and_publication(tmp_path, change, state):
    from scripts.download.company.company_failure_audit import AuditCompanyFailureObjects

    index = _published(tmp_path)
    with SqliteSession(index.path) as raw, SqliteTransaction(raw):
        if change == "body_checked":
            raw.run("UPDATE disclosures SET body_checked=0")
        elif change == "missing_body":
            raw.run("UPDATE disclosures SET body_hash=NULL")
        elif change == "revision":
            raw.run("UPDATE disclosures SET record_hash='changed'")
        elif change == "missing":
            raw.run("DELETE FROM disclosures")
        elif change == "population":
            raw.run("DELETE FROM company_population")
    with SqliteSession(index.facts_path) as formal, SqliteTransaction(formal):
        if change == "body_hash":
            formal.run("UPDATE company_input_receipt SET body_hash=?", (b"b" * 32,))
        elif change == "missing_body":
            formal.run("UPDATE company_input_receipt SET body_hash=NULL")
        elif change == "extraction_hash":
            formal.run("UPDATE company_input_receipt SET extraction_hash=?", (b"b" * 32,))
        elif change == "receipt_revision":
            formal.run("UPDATE company_input_receipt SET record_hash='changed'")
        elif change == "receipt_code":
            formal.run("UPDATE company_input_receipt SET code='600002'")
        elif change == "outcome":
            formal.run("UPDATE company_input_receipt SET outcome='disclosure_document_ocr_timeout_exhausted'")
    before = index.path.read_bytes(), index.facts_path.read_bytes()
    events = [{"supplier": "cninfo", "diagnostic": "operation_kind=download > object_id=first > revision=a"}] * 2
    report = AuditCompanyFailureObjects(
        index.path, index.facts_path, events, sqlite_policy=SqlitePolicyFixture(), cancel_requested=lambda: False,
    )
    assert report["stable_read"] and report["object_count"] == 1 and report["states"] == {state: 1}
    assert report["objects"][0]["events"] == 2
    assert bool(report["receipts"]) is (state == "published")
    assert (index.path.read_bytes(), index.facts_path.read_bytes()) == before


@pytest.mark.parametrize("diagnostic", [
    "stage=download;record=first;version=a", "object_id=first", "object_id=first > revision=a > revision=b",
    "object_id=first > revision=a > object_id=second", "object_id=first > revision=a/b",
])
def test_failure_identity_rejects_retired_or_ambiguous_diagnostics(diagnostic):
    from scripts.download.company.company_failure_audit import ParseCompanyFailureIdentity

    assert ParseCompanyFailureIdentity(diagnostic) is None


def test_failure_object_concurrent_change_cannot_be_reported_as_published(tmp_path, monkeypatch):
    from scripts.download.company import company_failure_audit

    index = _published(tmp_path)
    read = company_failure_audit.ReadCompanyPublicationIdentity

    def mutate(connection):
        with SqliteSession(index.path) as writer, SqliteTransaction(writer):
            writer.run("INSERT OR IGNORE INTO company_dirty_codes VALUES('600001')")
        return read(connection)

    monkeypatch.setattr(company_failure_audit, "ReadCompanyPublicationIdentity", mutate)
    report = company_failure_audit.AuditCompanyFailureObjects(
        index.path, index.facts_path, [{"supplier": "cninfo", "diagnostic": "object_id=first > revision=a"}],
        sqlite_policy=SqlitePolicyFixture(), cancel_requested=lambda: False,
    )
    assert not report["stable_read"] and report["states"] == {"unconfirmed": 1}


def test_shared_failure_object_requires_receipts_for_every_population_code(tmp_path):
    from scripts.download.company.company_failure_audit import AuditCompanyFailureObjects

    index = _published(tmp_path)
    with SqliteSession(index.path) as raw, SqliteTransaction(raw):
        raw.run("INSERT INTO company_population VALUES('600002')")
        raw.run("INSERT INTO disclosure_codes SELECT '600002',id FROM disclosures WHERE source_key='first'")
    report = AuditCompanyFailureObjects(
        index.path, index.facts_path, [{"supplier": "cninfo", "diagnostic": "object_id=first > revision=a"}],
        sqlite_policy=SqlitePolicyFixture(), cancel_requested=lambda: False,
    )
    assert report["states"] == {"pending_publication": 1} and len(report["receipts"]) == 1


@pytest.mark.parametrize("column", ["content_hash", "extraction_hash"])
def test_publication_change_between_audit_sections_invalidates_failure_recovery(tmp_path, monkeypatch, column):
    from scripts.download.company import company_query_audit

    index = _published(tmp_path)
    index.record_failure(DownloadFailureRecord(
        "fixture", fixtures.NOW, DownloadFailureContext("company_download", supplier="cninfo"), "timeout", "[]",
        "object_id=first > revision=a",
    ), configuration=DownloadFailureConfiguration(100, 50))
    audit = company_query_audit.AuditCompanyFailureObjects

    def change_after_snapshot(*args, **kwargs):
        result = audit(*args, **kwargs)
        with SqliteSession(index.facts_path) as writer, SqliteTransaction(writer):
            writer.run(f"UPDATE company_publication SET {column}=?", (b"b" * 32,))
        return result

    monkeypatch.setattr(company_query_audit, "AuditCompanyFailureObjects", change_after_snapshot)
    report = AuditCompanyDatabase(
        index.path, facts_path=index.facts_path, now=fixtures.NOW, budget_seconds=10, sqlite_policy=SqlitePolicyFixture(),
    )
    assert "company_failure_audit_concurrent_change" in report["findings"]
    failures = report["summary"]["database"]["failure_objects"]
    assert not failures["stable_read"] and failures["states"] == {"unconfirmed": 1}


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
    report = BuildReport("company-resume", [DiagnosticResult(
        "company_resume", 0, 1.0, {"schema_version": "company-resume", "status": "degraded",
                                 "summary": {"database": {"publication": publication}}}, None,
    )])
    assert "must-not-project" not in json.dumps(report)
    assert "company_components_incomplete" in json.dumps(report)
    fields = report["checks"][0]["summary"]["database"]["publication"]["field_audit"]
    assert fields["invalid_value_types"] == publication["field_audit"]["invalid_value_types"]


def test_field_recovery_preview_names_missing_field_without_writing_or_inventing_material(tmp_path):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("plan", "控股股东减持计划公告")
    partition = index.partitions(("sse",), fixtures.NOW)[0]
    index.commit_page(partition, DisclosurePage((item,), 1, False), fixtures.NOW)
    index.commit_document(item, fixtures._extraction(
        "本公司控股股东计划减持股份，占公司总股本的比例不超过1%。"
    ), fixtures.NOW)
    index.publish(item.codes, fixtures.NOW)
    with SqliteSession(index.facts_path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE company_reduction_plan SET planned_pct=NULL")
    before = (index.path.read_bytes(), index.facts_path.read_bytes())
    result = _audit(index)
    plan = result["field_audit"]["recovery_plan"]
    ratio = next(item for item in plan["items"] if item["field"] == "planned_pct")
    assert ratio["source_key"] == "plan"
    assert (ratio["next_step"], ratio["reason"]) == ("verify", "same_rule_already_attempted")
    assert not ratio["network_required"]
    assert plan["read_only"]
    assert (index.path.read_bytes(), index.facts_path.read_bytes()) == before
    from scripts.download.company.company_field_audit import ProjectCompanyFieldAudit

    ratio["raw_body"] = "must-not-project"
    projected = ProjectCompanyFieldAudit(result["field_audit"])
    assert "must-not-project" not in json.dumps(projected)


def test_local_material_inspection_verifies_text_and_compares_formal_contract_without_writes(tmp_path):
    from scripts.download.company.company_field_audit import InspectCompanyFieldMaterial
    from tests.helpers.download.company.company_annual_income import _contract

    index = fixtures._index(tmp_path)
    _contract(index)
    before = (index.path.read_bytes(), index.facts_path.read_bytes())
    report = InspectCompanyFieldMaterial(index.path, index.facts_path, ("contract-income",), sqlite_policy=SqlitePolicyFixture(),
                                        observed_at=fixtures.NOW, event_policy=fixtures.CompanyEventPolicy())
    assert report["stable_read"] and report["read_only"] and not report["field_correctness_verified"]
    item = report["items"][0]
    assert item["material_available"] and item["current_receipt"]
    assert len(item["body_hash"]) == 64 and len(item["excerpts"]) <= 16
    assert all(len(excerpt["text"]) <= 150 for excerpt in item["excerpts"])
    assert item["contracts"][0]["contract_amount"] == 200_000_000
    assert item["contracts"][0]["annual_revenue"] is None
    assert (index.path.read_bytes(), index.facts_path.read_bytes()) == before


@pytest.mark.parametrize("keys", ((), ("x",) * 7, ("duplicate", "duplicate")))
def test_material_inspection_rejects_unbounded_or_duplicate_keys_before_io(tmp_path, keys):
    from scripts.download.company.company_field_audit import InspectCompanyFieldMaterial

    with pytest.raises(ValueError, match="company_material_keys_invalid"):
        InspectCompanyFieldMaterial(tmp_path / "missing.sqlite3", tmp_path / "missing-facts.sqlite3", keys, sqlite_policy=SqlitePolicyFixture(),
                                    observed_at=fixtures.NOW, event_policy=fixtures.CompanyEventPolicy())
    assert not tuple(tmp_path.iterdir())


def test_material_inspection_does_not_count_a_previous_extraction_receipt_as_current(tmp_path):
    from scripts.download.company.company_field_audit import InspectCompanyFieldMaterial
    from tests.helpers.download.company.company_annual_income import _contract

    index = fixtures._index(tmp_path)
    _contract(index)
    with SqliteSession(index.facts_path) as formal, SqliteTransaction(formal, immediate=True):
        formal.run("UPDATE company_input_receipt SET extraction_hash=? WHERE outcome='valid'", (b"x" * 32,))
    before = (index.path.read_bytes(), index.facts_path.read_bytes())
    report = InspectCompanyFieldMaterial(index.path, index.facts_path, ("contract-income",), sqlite_policy=SqlitePolicyFixture(),
                                        observed_at=fixtures.NOW, event_policy=fixtures.CompanyEventPolicy())
    assert report["stable_read"] and report["items"][0]["material_available"]
    assert report["items"][0]["current_receipt"] is False
    assert (index.path.read_bytes(), index.facts_path.read_bytes()) == before
