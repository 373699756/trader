"""Old risk identities remain distinguishable from missing or changed evidence."""

import json
import sqlite3
import time
from dataclasses import replace

import pytest

from scripts.core.catalog import SelectionArguments
from scripts.core.contracts import DiagnosticResult
from scripts.core.projections import BuildReport
from scripts.download.company import company_query_audit
from scripts.download.company import company_risk_comparison
from scripts.download.company.company_risk_comparison import CompareCompanyRisks, ProjectCompanyRiskComparison, WriteCompanyRiskBaseline
from tests.helpers.download.company import company_evidence as collection_fixtures
from tests.helpers.download.company.company_startup import _AT, _snapshot
from tests.helpers.download.company_resources import SQLiteCompanyFactsIndexFixture
from tests.helpers.download.configuration import CompanyEventPolicyFixture
from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.shared.tool_cli import BuildToolCommands, ParseBusinessOptions, ParseToolArguments
from trader.download.domain.company_facts import CompileCompanyFacts
from trader.download.infra.company_facts_index import CompanyInputReceipt, ReadCompanyPublicationIdentity
from trader.download.infra.company_facts_protocol import EncodeCompanyTime
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession


def _fixture(tmp_path):
    writer = SQLiteCompanyFactsIndexFixture(tmp_path / "facts.sqlite3")
    writer.initialize()
    facts = CompileCompanyFacts(_snapshot(), CompanyEventPolicyFixture())
    receipt = CompanyInputReceipt("cninfo", "investigation", facts.code, "revision-1", writer.extraction_hash, b"b" * 32, _AT)
    writer.publish((facts,), {"investigation": "cninfo"}, None, _AT, receipts=(receipt,))
    with SqliteSession(
        writer.path, SqliteConnectionOptions(read_only=True), sqlite_policy=SqlitePolicyFixture(),
    ) as connection:
        risks = [dict(row) for row in connection.run("SELECT * FROM company_risk_event")]
        links = [dict(row) for row in connection.run(
            "SELECT r.code,r.event_key,e.source,e.source_key FROM company_risk_event r "
            "JOIN company_risk_evidence re ON re.risk_id=r.id "
            "JOIN company_evidence_ref e ON e.id=re.evidence_id"
        )]
        publication = ReadCompanyPublicationIdentity(connection)
    assert len(risks) == len(links) == 1
    links[0]["receipts"] = [{
        "record_hash": receipt.record_hash, "extraction_hash": receipt.extraction_hash.hex(),
        "body_hash": receipt.body_hash.hex(), "published_at": EncodeCompanyTime(_AT), "policy_id": "",
    }]
    return writer.path, {
        "publication": {"content_hash": publication.content_hash.hex(),
                        "extraction_hash": publication.extraction_hash.hex(),
                        "published_at": publication.published_at.isoformat()},
        "risks": risks, "evidence": links,
    }


def _compare(path, baseline_path):
    return CompareCompanyRisks(
        path, baseline_path, deadline=time.monotonic() + 5,
        sqlite_policy=SqlitePolicyFixture(), cancel_requested=lambda: False,
    )


def test_exported_risk_baseline_round_trips_without_changing_source_or_overwriting(tmp_path):
    source, expected = _fixture(tmp_path)
    before = source.read_bytes()
    baseline = tmp_path / "baseline.json"
    WriteCompanyRiskBaseline(source, baseline, deadline=time.monotonic() + 5, sqlite_policy=SqlitePolicyFixture())
    comparison = _compare(source, baseline)
    assert comparison.all_old_risks_and_evidence_preserved and comparison.exactly_preserved == 1
    assert source.read_bytes() == before
    payload = json.loads(baseline.read_text())
    assert payload["evidence"] == expected["evidence"] and "id" not in payload["risks"][0]
    saved = baseline.read_bytes()
    with pytest.raises(FileExistsError):
        WriteCompanyRiskBaseline(source, baseline, deadline=time.monotonic() + 5, sqlite_policy=SqlitePolicyFixture())
    assert baseline.read_bytes() == saved


@pytest.mark.parametrize("limit", ("_RISK_LIMIT", "_EVIDENCE_LIMIT", "_BASELINE_BYTES"))
def test_export_rejects_limit_before_creating_baseline(tmp_path, monkeypatch, limit):
    source, _ = _fixture(tmp_path)
    monkeypatch.setattr(company_risk_comparison, limit, 0)
    target = tmp_path / "baseline.json"
    with pytest.raises(ValueError, match="limit"):
        WriteCompanyRiskBaseline(source, target, deadline=time.monotonic() + 5, sqlite_policy=SqlitePolicyFixture())
    assert not target.exists()


def test_export_deadline_does_not_create_or_modify_files(tmp_path):
    source, _ = _fixture(tmp_path)
    before = source.read_bytes()
    target = tmp_path / "baseline.json"
    with pytest.raises(TimeoutError, match="deadline_exceeded"):
        WriteCompanyRiskBaseline(source, target, deadline=time.monotonic() - 1, sqlite_policy=SqlitePolicyFixture())
    assert not target.exists() and source.read_bytes() == before


@pytest.mark.parametrize("change", ("none", "business", "evidence", "missing", "revision", "body", "receipt_age", "unconfirmed"))
def test_risk_comparison_preserves_ids_and_business_evidence_without_writing(tmp_path, change):
    path, baseline = _fixture(tmp_path)
    risk = baseline["risks"][0]
    risk["id"] += 1000
    if change == "business":
        risk["case_number"] = "private original case content"
    elif change == "evidence":
        baseline["evidence"][0]["source_key"] = "original-evidence-not-in-current-publication"
    elif change == "missing":
        risk["event_key"] = baseline["evidence"][0]["event_key"] = "missing-original-risk"
    elif change == "revision":
        baseline["evidence"][0]["receipts"][0]["record_hash"] = "previous-revision"
    elif change == "body":
        baseline["evidence"][0]["receipts"][0]["body_hash"] = "a" * 64
    elif change == "receipt_age":
        baseline["evidence"][0]["receipts"][0]["published_at"] -= 1
    elif change == "unconfirmed":
        baseline["evidence"][0]["receipts"] = []
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    before = path.read_bytes()
    result = _compare(path, baseline_path)
    report = ProjectCompanyRiskComparison(result)
    assert path.read_bytes() == before
    assert result.baseline_risks == result.new_risks == 1
    assert result.stable_read
    assert result.exactly_preserved == int(change == "none")
    assert result.all_old_risks_and_evidence_preserved is (change == "none")
    assert report["missing_means_resolved"] is False
    assert "private original case content" not in json.dumps(report)
    assert "original-evidence-not-in-current-publication" not in json.dumps(report)
    if change in ("business", "evidence", "revision", "body", "receipt_age", "unconfirmed"):
        assert len(result.changed_requires_review) == 1
        changed = result.changed_requires_review[0]
        assert changed.changed_fields == (("case_number",) if change == "business" else ())
        assert changed.original_evidence_retained is (change in ("business", "unconfirmed"))
        assert changed.evidence_identity_confirmed is (change != "unconfirmed")
    if change == "missing":
        assert result.missing == ((risk["code"], "missing-original-risk"),)


@pytest.mark.parametrize("invalid", (
    "duplicate", "orphan_evidence", "no_evidence", "bool", "category", "field_missing", "old_structure",
    "receipt_missing", "receipt_digest", "receipt_bool", "duplicate_receipt", "duplicate_evidence", "publication",
))
def test_invalid_risk_baseline_is_rejected_without_repair_or_writes(tmp_path, invalid):
    path, baseline = _fixture(tmp_path)
    if invalid == "duplicate":
        baseline["risks"].append(dict(baseline["risks"][0]))
    elif invalid == "orphan_evidence":
        baseline["evidence"][0]["event_key"] = "unknown-risk"
    elif invalid == "no_evidence":
        baseline["evidence"].clear()
    elif invalid == "bool":
        baseline["risks"][0]["risk_type"] = True
    elif invalid == "category":
        baseline["risks"][0]["status"] = 9
    elif invalid == "field_missing":
        del baseline["risks"][0]["effective_at"]
    elif invalid == "old_structure":
        del baseline["publication"]
    elif invalid == "receipt_missing":
        del baseline["evidence"][0]["receipts"]
    elif invalid == "receipt_digest":
        baseline["evidence"][0]["receipts"][0]["body_hash"] = "wrong"
    elif invalid == "receipt_bool":
        baseline["evidence"][0]["receipts"][0]["published_at"] = True
    elif invalid == "duplicate_receipt":
        baseline["evidence"][0]["receipts"] *= 2
    elif invalid == "duplicate_evidence":
        baseline["evidence"] *= 2
    elif invalid == "publication":
        baseline["publication"]["published_at"] = "2026-11-16T10:00:00"
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="company_risk_baseline_"):
        _compare(path, baseline_path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("proof", ("no_receipt", "no_body", "failed_only"))
def test_existing_edges_without_original_valid_body_proof_remain_unconfirmed(tmp_path, proof):
    path, _ = _fixture(tmp_path)
    with sqlite3.connect(path) as connection:
        if proof == "no_receipt":
            connection.execute("DELETE FROM company_input_receipt")
        elif proof == "no_body":
            connection.execute("UPDATE company_input_receipt SET body_hash=NULL")
        else:
            connection.execute("UPDATE company_input_receipt SET outcome='field_attempted'")
    baseline_path = tmp_path / "baseline.json"
    WriteCompanyRiskBaseline(path, baseline_path, deadline=time.monotonic() + 5, sqlite_policy=SqlitePolicyFixture())
    result = _compare(path, baseline_path)
    assert result.exactly_preserved == 0 and not result.all_old_risks_and_evidence_preserved
    change, = result.changed_requires_review
    assert change.original_evidence_retained and not change.evidence_identity_confirmed
    assert ProjectCompanyRiskComparison(result)["changed_requires_review"][0]["evidence_identity_confirmed"] is False


def test_new_receipt_keeps_original_proof_and_changed_extraction_does_not_erase_it(tmp_path):
    path, _ = _fixture(tmp_path)
    baseline_path = tmp_path / "baseline.json"
    WriteCompanyRiskBaseline(path, baseline_path, deadline=time.monotonic() + 5, sqlite_policy=SqlitePolicyFixture())
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO company_input_receipt SELECT source,source_key,code,'revision-2',?,body_hash,published_at,outcome,policy_id "
            "FROM company_input_receipt", (b"n" * 32,),
        )
        connection.execute("UPDATE company_publication SET content_hash=?,extraction_hash=?", (b"n" * 32, b"n" * 32))
    result = _compare(path, baseline_path)
    assert result.all_old_risks_and_evidence_preserved
    assert result.baseline_publication_hash != result.publication_hash
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM company_input_receipt WHERE record_hash='revision-1'")
    lost = _compare(path, baseline_path)
    assert not lost.all_old_risks_and_evidence_preserved
    assert not lost.changed_requires_review[0].original_evidence_retained


def test_export_with_missing_risk_edge_is_rejected_before_creating_target(tmp_path):
    path, _ = _fixture(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM company_risk_evidence")
    target = tmp_path / "baseline.json"
    with pytest.raises(ValueError, match="original_evidence_missing"):
        WriteCompanyRiskBaseline(path, target, deadline=time.monotonic() + 5, sqlite_policy=SqlitePolicyFixture())
    assert not target.exists()


def test_expired_budget_precedes_missing_baseline_file_and_database_access(tmp_path):
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        CompareCompanyRisks(
            tmp_path / "missing.sqlite3", tmp_path / "missing.json", deadline=0,
            sqlite_policy=SqlitePolicyFixture(), cancel_requested=lambda: False,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("cancel_phase", ("after_read", "after_parse", "between_risks", "between_evidence"))
def test_baseline_read_parse_and_rows_observe_the_same_cancellation(tmp_path, monkeypatch, cancel_phase):
    risk = {
        "code": "600001", "event_key": "first", "risk_type": 1, "subject_type": 1, "level": 1,
        "case_number": None, "first_disclosed_at": 1, "last_disclosed_at": 1, "effective_at": 1,
        "status": 1, "resolved_at": None, "expires_at": None,
    }
    baseline = {"risks": [risk, dict(risk, event_key="second")], "evidence": [
        {"code": "600001", "event_key": key, "source": 1, "source_key": key, "receipts": []} for key in ("first", "second")
    ], "publication": {"content_hash": "a" * 64, "extraction_hash": "b" * 64, "published_at": _AT.isoformat()}}
    content = json.dumps(baseline).encode()
    phase = "start"
    identities = 0
    original_loads = json.loads
    original_record = company_risk_comparison._record
    original_identity = company_risk_comparison._identity

    def read(*args, **kwargs):
        nonlocal phase
        phase = "after_read"
        return content

    def parse(*args, **kwargs):
        nonlocal phase
        result = original_loads(*args, **kwargs)
        phase = "after_parse"
        return result

    def record(row):
        nonlocal phase
        result = original_record(row)
        phase = "between_risks"
        return result

    def identity(row):
        nonlocal phase, identities
        result = original_identity(row)
        identities += 1
        if identities > 2:
            phase = "between_evidence"
        return result

    monkeypatch.setattr(company_risk_comparison, "ReadRegularFile", read)
    monkeypatch.setattr(company_risk_comparison.json, "loads", parse)
    monkeypatch.setattr(company_risk_comparison, "_record", record)
    monkeypatch.setattr(company_risk_comparison, "_identity", identity)
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        CompareCompanyRisks(
            tmp_path / "missing.sqlite3", tmp_path / "baseline.json", deadline=time.monotonic() + 5,
            sqlite_policy=SqlitePolicyFixture(), cancel_requested=lambda: phase == cancel_phase,
        )
    assert phase == cancel_phase and not (tmp_path / "missing.sqlite3").exists()


def test_actual_sqlite_connection_timeout_is_capped_by_remaining_budget(tmp_path, monkeypatch):
    path, baseline = _fixture(tmp_path)
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    original_connect = sqlite3.connect
    observed = []

    def connect(*args, **kwargs):
        observed.append(kwargs["timeout"])
        return original_connect(*args, **kwargs)

    policy = replace(SqlitePolicyFixture(), connection_timeout_seconds=12)
    monkeypatch.setattr(company_risk_comparison.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(sqlite3, "connect", connect)
    result = CompareCompanyRisks(
        path, baseline_path, deadline=100.25, sqlite_policy=policy, cancel_requested=lambda: False,
    )
    assert result.all_old_risks_and_evidence_preserved
    assert observed == [0.25]
    assert policy.connection_timeout_seconds == 12


def test_baseline_is_a_bounded_regular_file_and_cancelled_reads_do_not_create_database(tmp_path):
    baseline = tmp_path / "oversize.json"
    with baseline.open("wb") as stream:
        stream.truncate(4 * 1024 * 1024 + 1)
    database = tmp_path / "missing.sqlite3"
    with pytest.raises(ValueError, match="regular_file_size_limit"):
        _compare(database, baseline)
    baseline.write_text(json.dumps({"publication": {"content_hash": "a" * 64, "extraction_hash": "b" * 64,
                                                   "published_at": _AT.isoformat()}, "risks": [], "evidence": []}), encoding="utf-8")
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        CompareCompanyRisks(
            database, baseline, deadline=time.monotonic() + 5,
            sqlite_policy=SqlitePolicyFixture(), cancel_requested=lambda: True,
        )
    assert not database.exists()
    linked = tmp_path / "linked.json"
    linked.symlink_to(baseline)
    with pytest.raises(ValueError, match="regular_file_required"):
        _compare(database, linked)


def test_only_read_only_database_audit_accepts_baseline_and_forwards_it(tmp_path):
    selection = SelectionArguments("company-resume")
    baseline = tmp_path / "baseline.json"
    args = ParseToolArguments([*selection, "--company-database-audit", "--company-risk-baseline", str(baseline)])
    options = ParseBusinessOptions(args)
    assert options.company_risk_baseline == baseline
    command = BuildToolCommands(options)[0]
    assert "--risk-baseline" in command.argv and "--database-only" in command.argv
    for mode in ((), ("--company-query-audit",), ("--company-document-audit",)):
        args = ParseToolArguments([*selection, *mode, "--company-risk-baseline", str(baseline)])
        with pytest.raises(ValueError, match="requires read-only"):
            ParseBusinessOptions(args)


def test_comparison_reaches_public_report_whitelist(tmp_path):
    index = collection_fixtures._index(tmp_path)
    index.publish(("600001",), collection_fixtures.NOW)
    baseline = tmp_path / "baseline.json"
    WriteCompanyRiskBaseline(index.facts_path, baseline, deadline=time.monotonic() + 5, sqlite_policy=SqlitePolicyFixture())
    report = company_query_audit.AuditCompanyDatabase(
        index.path, now=collection_fixtures.NOW, budget_seconds=30, facts_path=index.facts_path,
        sqlite_policy=SqlitePolicyFixture(), risk_baseline=baseline,
    )
    result = DiagnosticResult("company_resume", 0, 10, report, None)
    public = BuildReport("company-resume", [result])
    projected = public["checks"][0]["summary"]["database"]["risk_comparison"]
    assert projected["baseline_risks"] == 0
    assert projected["all_old_risks_and_evidence_preserved"]
    assert projected["missing"] == projected["changed_requires_review"] == []
    assert projected["missing_means_resolved"] is False


def test_comparison_budget_uses_existing_audit_deadline_classification(tmp_path, monkeypatch):
    index = collection_fixtures._index(tmp_path)
    index.publish(("600001",), collection_fixtures.NOW)
    baseline = tmp_path / "baseline.json"
    WriteCompanyRiskBaseline(index.facts_path, baseline, deadline=time.monotonic() + 5, sqlite_policy=SqlitePolicyFixture())

    def interrupted(*args, cancel_requested, **kwargs):
        monkeypatch.setattr(company_query_audit.time, "monotonic", lambda: float("inf"))
        assert cancel_requested()
        raise sqlite3.OperationalError("interrupted")

    monkeypatch.setattr(company_query_audit, "CompareCompanyRisks", interrupted)
    report = company_query_audit.AuditCompanyDatabase(
        index.path, now=collection_fixtures.NOW, budget_seconds=30, facts_path=index.facts_path,
        sqlite_policy=SqlitePolicyFixture(), risk_baseline=baseline,
    )
    assert report["status"] == "failed"
    assert report["findings"] == ["company_database_audit_budget_exhausted"]
