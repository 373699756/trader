"""Field delivery diagnostics keep explicit isolation and bounded public output."""

import json

import pytest

from scripts.core.contracts import DiagnosticResult
from scripts.core.projections import BuildReport
from scripts.download.company.cli import BuildParser, ParseOptions
from scripts.download.company.commands import BuildCommands


def test_field_migration_public_mode_requires_external_directory_and_preserves_worker_flag(tmp_path):
    parser = BuildParser("company-resume")
    with pytest.raises(ValueError, match="requires a new external"):
        ParseOptions(parser.parse_args(["--company-field-migration-audit"]))
    options = ParseOptions(parser.parse_args([
        "--company-field-migration-audit", "--company-replay-directory", str(tmp_path / "candidate"),
    ]))
    command, = BuildCommands(options)
    assert "--field-migration-only" in command.argv
    assert "--directory" in command.argv
    assert options.company_field_migration_audit


def test_field_migration_public_mode_rejects_symbolic_link_before_path_resolution(tmp_path):
    destination = tmp_path / "actual"
    destination.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(destination, target_is_directory=True)
    parser = BuildParser("company-resume")
    with pytest.raises(ValueError, match="symbolic links"):
        ParseOptions(parser.parse_args([
            "--company-field-migration-audit", "--company-replay-directory", str(link),
        ]))


def test_field_migration_cannot_be_combined_with_other_company_modes(tmp_path):
    with pytest.raises(SystemExit):
        BuildParser("company-resume").parse_args([
            "--company-field-migration-audit", "--company-database-audit",
            "--company-replay-directory", str(tmp_path / "candidate"),
        ])


def test_field_replay_public_mode_preserves_explicit_keys_and_requires_isolation(tmp_path):
    parser = BuildParser("company-resume")
    with pytest.raises(ValueError, match="requires a new external"):
        ParseOptions(parser.parse_args(["--company-field-replay-keys", "first"]))
    options = ParseOptions(parser.parse_args([
        "--company-field-replay-keys", "first", "second",
        "--company-replay-directory", str(tmp_path / "replay"),
    ]))
    command, = BuildCommands(options)
    offset = command.argv.index("--field-replay-keys")
    assert command.argv[offset + 1:offset + 3] == ("first", "second")


@pytest.mark.parametrize("keys", [("same", "same"), ("one two",), tuple(str(index) for index in range(7))])
def test_field_replay_rejects_ambiguous_or_unbounded_key_requests(tmp_path, keys):
    with pytest.raises(ValueError, match="one to six distinct"):
        ParseOptions(BuildParser("company-resume").parse_args([
            "--company-field-replay-keys", *keys, "--company-replay-directory", str(tmp_path / "replay"),
        ]))


def test_material_field_conclusions_bound_members_and_do_not_publish_full_source_text():
    proof = {"source_key": "source", "start_line": 2, "end_line": 4, "quote": "PRIVATE_FULL_TEXT"}
    conclusion = {"event_id": "event", "field": "actual_pct", "state": "verified", "value": 0.3,
                  "applicable": True, "reason": "source_field_located", "evidence": [proof] * 20,
                  "raw_body": "PRIVATE_FULL_TEXT"}
    payload = {"schema_version": "company-field-material", "status": "observed", "read_only": True,
               "stable_read": True, "field_correctness_verified": False, "missing_source_keys": [],
               "items": [{"field_conclusions": [conclusion] * 100}]}
    report = BuildReport("company-resume", (DiagnosticResult("company_resume", 0, 0.1, payload, None),))
    item = report["checks"][0]["summary"]["items"][0]
    assert len(item["field_conclusions"]) == 32 and item["field_conclusions_truncated"]
    assert len(item["field_conclusions"][0]["evidence"]) == 4
    assert item["field_conclusions"][0]["value"] == 0.3
    assert "PRIVATE_FULL_TEXT" not in json.dumps(report)


@pytest.mark.parametrize("missing", [None, "business_data_unchanged", "original_receipts_unchanged", "target_reader_verified"])
def test_migration_report_requires_actual_isolated_preservation_and_does_not_authorize_production(missing):
    payload = {
        "schema_version": "company-field-migration", "status": "passed", "read_only_source": True,
        "isolated_copy": True, "production_authorized": False, "source_schema": 3, "target_schema": 4,
        "source_data_writes": 0, "field_correctness_verified": False,
        "source_input_unchanged": True, "business_data_unchanged": True, "original_receipts_unchanged": True,
        "target_reader_verified": True, "target_integrity_verified": True, "target_foreign_keys_verified": True,
        "source_files_sha256_before": {"PRIVATE_SOURCE_PATH": "PRIVATE_SOURCE_PAYLOAD"},
    }
    if missing is not None:
        payload.pop(missing)
    report = BuildReport("company-resume", (DiagnosticResult("company_resume", 0, 0.1, payload, None),))
    assert report["status"] == ("passed" if missing is None else "failed")
    assert report["checks"][0]["summary"]["production_authorized"] is False
    assert "PRIVATE_SOURCE" not in json.dumps(report)


def test_migration_projection_limits_state_and_table_counts_and_rejects_raw_nested_values():
    payload = {
        "schema_version": "company-field-migration", "status": "failed", "read_only_source": True,
        "isolated_copy": True, "production_authorized": False, "source_schema": 3, "target_schema": 4,
        "field_correctness_verified": False, "fields": {"PRIVATE_RAW": ["PRIVATE_RAW"] * 1000},
        "business_data_unchanged": {"PRIVATE_RAW": "PRIVATE_RAW"},
        "field_states": {"present_unverified": 7, "verified": True, "not_disclosed": 10**40,
                         "PRIVATE_RAW": {"PRIVATE_RAW": "PRIVATE_RAW"}},
        "preserved_business_tables": {"company_facts": 2, "company_review": -1,
                                      "PRIVATE_RAW": ["PRIVATE_RAW"] * 1000},
    }
    report = BuildReport("company-resume", (DiagnosticResult("company_resume", 1, 0.1, payload, None),))
    summary = report["checks"][0]["summary"]
    assert summary["field_states"] == {"present_unverified": 7}
    assert summary["preserved_business_tables"] == {"company_facts": 2}
    assert summary["fields"] is None and summary["business_data_unchanged"] is False
    assert "PRIVATE_RAW" not in json.dumps(report)


def test_field_replay_projection_bounds_fields_and_keeps_physical_source_change_explicit():
    field = {"code": "600001", "event_id": "event", "field": "actual_pct", "value": 0.3,
             "state": "verified", "applicable": True, "evidence": [{"quote": "PRIVATE_RAW"}] * 20}
    payload = {
        "schema_version": "company-field-replay", "status": "degraded", "read_only_source": True,
        "isolated_copy": True, "production_authorized": False, "field_correctness_verified": False,
        "full_market_verified": False, "performance_verified": False, "network_requests": 0, "source_data_writes": 0,
        "source_empty_wal_created": True, "collection_source_files_unchanged": False,
        "field_items": [field] * 200, "field_states": {"verified": 2, "PRIVATE_RAW": {"a": "PRIVATE_RAW"}},
        "codes": ["600001"] * 100, "source_keys": ["key"] * 100,
        "collection_source_files_sha256_before": ["a" * 64, None, "PRIVATE_RAW"],
    }
    report = BuildReport("company-resume", (DiagnosticResult("company_resume", 0, 0.1, payload, None),))
    summary = report["checks"][0]["summary"]
    assert len(summary["field_items"]) == 100 and len(summary["field_items"][0]["evidence"]) == 4
    assert len(summary["codes"]) == len(summary["source_keys"]) == 6
    assert summary["field_states"] == {"verified": 2}
    assert summary["source_empty_wal_created"] is True and summary["collection_source_files_unchanged"] is False
    assert summary["collection_source_files_sha256_before"] == {"database": "a" * 64, "wal": None, "receipt": None}
    assert summary["production_authorized"] is False and "PRIVATE_RAW" not in json.dumps(report)


@pytest.mark.parametrize("changes", (
    {"second_new_field_attempted": False},
    {"missing_texts": 1},
    {"round_2_publication_text_reads": 2},
))
def test_replay_pass_requires_strict_zero_counts_without_contradictory_evidence(changes):
    payload = {
        "schema_version": "company-field-replay", "status": "passed", "read_only_source": True,
        "isolated_copy": True, "production_authorized": False, "field_correctness_verified": False,
        "full_market_verified": False, "performance_verified": False, "network_requests": 0, "source_data_writes": 0,
        "source_input_unchanged": True, "collection_backup_generation_stable": True,
        "original_valid_receipts_unchanged": True, "second_fields_and_publication_unchanged": True,
        "second_zero_publication_text_reads": True, "migration_status": "passed",
        "second_new_field_attempted": 0, "missing_texts": 0, "round_2_publication_text_reads": 0,
    }
    valid = BuildReport("company-resume", (DiagnosticResult("company_resume", 0, 0.1, payload, None),))
    assert valid["status"] == "passed"

    payload.update(changes)
    rejected = BuildReport("company-resume", (DiagnosticResult("company_resume", 0, 0.1, payload, None),))
    assert rejected["status"] == "failed"
    assert rejected["checks"][0]["summary"]["field_correctness_verified"] is False


@pytest.mark.parametrize("guard", ["source", "record", "body", "time", "current"])
def test_material_conclusions_are_bound_to_the_exact_current_source_version(tmp_path, monkeypatch, guard):
    from dataclasses import replace
    from datetime import datetime

    from scripts.download.company import company_field_audit
    from tests.helpers.download.company import company_evidence as fixtures
    from tests.helpers.download.company.company_annual_income import _contract
    from tests.helpers.shared.configuration import SqlitePolicyFixture
    from trader.download.domain.company_events import CompanySubject
    from trader.download.domain.company_field_results import CompanyFieldEvidence
    from trader.infra.sqlite_connection import SqliteSession

    index = fixtures._index(tmp_path)
    _contract(index)
    with SqliteSession(index.path) as raw, SqliteSession(index.facts_path) as formal:
        row = raw.run("SELECT record_hash,body_hash,published_at FROM disclosures WHERE source_key='contract-income'").require_one()
        results = company_field_audit.ReadCompanyFieldResults(formal, ("600001",))
    proof = CompanyFieldEvidence(
        "sse" if guard == "source" else "cninfo", "contract-income", "old" if guard == "record" else row[0],
        b"x" * 32 if guard == "body" else row[1], "explicit_cny_contract_amount:2",
        fixtures.NOW if guard == "time" else datetime.fromisoformat(row[2]),
        1, 1, "合同金额人民币2亿元。", "cny", CompanySubject.ISSUER,
    )
    projected = tuple(replace(item, state="verified", evidence=(proof,)) if item.field == "contract_amount" else item
                      for item in results)
    monkeypatch.setattr(company_field_audit, "ReadCompanyFieldResults", lambda *_: projected)
    report = company_field_audit.InspectCompanyFieldMaterial(
        index.path, index.facts_path, ("contract-income",), sqlite_policy=SqlitePolicyFixture(),
        observed_at=fixtures.NOW, event_policy=fixtures.CompanyEventPolicy(),
    )
    assert bool(report["items"][0]["field_conclusions"]) is (guard == "current")
