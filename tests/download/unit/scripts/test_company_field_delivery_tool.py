"""Field delivery diagnostics keep explicit isolation and bounded public output."""

import json

import pytest

from scripts.core.contracts import DiagnosticResult
from scripts.core.projections import BuildReport
from scripts.download.company.cli import BuildParser, ParseOptions
from scripts.download.company.commands import BuildCommands


def test_field_schema_migration_flag_is_not_supported():
    with pytest.raises(SystemExit):
        BuildParser("company-resume").parse_args(["--company-field-migration-audit"])








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


def test_recovered_text_requires_one_explicit_replay_key_and_preserves_file_argument(tmp_path):
    supplied = str(tmp_path / "source.txt")
    arguments = ["--company-recovered-text", supplied, "--company-replay-directory", str(tmp_path / "replay")]
    with pytest.raises(ValueError, match="exactly one"):
        ParseOptions(BuildParser("company-resume").parse_args(arguments))
    with pytest.raises(ValueError, match="exactly one"):
        ParseOptions(BuildParser("company-resume").parse_args(arguments + ["--company-field-replay-keys", "a", "b"]))
    options = ParseOptions(BuildParser("company-resume").parse_args(arguments + ["--company-field-replay-keys", "a"]))
    command, = BuildCommands(options)
    position = command.argv.index("--recovered-text")
    assert command.argv[position + 1] == supplied


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
        "round_1_publication_material_reads": [{"body_hash": "b" * 64, "reads": 2, "quote": "PRIVATE_RAW"}] * 20,
    }
    report = BuildReport("company-resume", (DiagnosticResult("company_resume", 0, 0.1, payload, None),))
    summary = report["checks"][0]["summary"]
    assert len(summary["field_items"]) == 100 and len(summary["field_items"][0]["evidence"]) == 4
    assert len(summary["codes"]) == len(summary["source_keys"]) == 6
    assert summary["field_states"] == {"verified": 2}
    assert summary["source_empty_wal_created"] is True and summary["collection_source_files_unchanged"] is False
    assert summary["collection_source_files_sha256_before"] == {"database": "a" * 64, "wal": None, "receipt": None}
    assert summary["production_authorized"] is False and "PRIVATE_RAW" not in json.dumps(report)
    assert len(summary["round_1_publication_material_reads"]) == 10
    assert summary["round_1_publication_material_reads"][0] == {"body_hash": "b" * 64, "reads": 2}


@pytest.mark.parametrize("changes", (
    {"second_new_field_attempted": False},
    {"missing_texts": 1},
    {"round_2_publication_text_reads": 2},
    {"original_risks_and_evidence_preserved": False},
    {"unselected_facts_unchanged": False},
    {"round_1_risk_comparison": {"all_old_risks_and_evidence_preserved": False}},
))
def test_replay_pass_requires_strict_zero_counts_without_contradictory_evidence(changes):
    payload = {
        "schema_version": "company-field-replay", "status": "passed", "read_only_source": True,
        "isolated_copy": True, "production_authorized": False, "field_correctness_verified": False,
        "full_market_verified": False, "performance_verified": False, "network_requests": 0, "source_data_writes": 0,
        "source_input_unchanged": True, "collection_backup_generation_stable": True,
        "original_valid_receipts_unchanged": True, "second_fields_and_publication_unchanged": True,
        "second_zero_publication_text_reads": True, "facts_backup_generation_stable": True,
        "second_new_field_attempted": 0, "missing_texts": 0, "round_2_publication_text_reads": 0,
        "original_risks_and_evidence_preserved": True, "unselected_facts_unchanged": True,
        **{f"round_{number}_risk_comparison": {
            "all_old_risks_and_evidence_preserved": True, "missing": [], "changed_requires_review": [],
        } for number in (1, 2)},
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
        b"x" * 32 if guard == "body" else row[1], "explicit_cny_contract_amount:3",
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
    amounts = [item for item in report["items"][0]["field_conclusions"] if item["field"] == "contract_amount"]
    assert bool(amounts) is (guard == "current")
