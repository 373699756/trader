"""Local replay uses real SQLite publication and never fetches missing text."""

import hashlib
import json
import socket
from datetime import timedelta
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.download.company import company_field_replay as replay
from scripts.download.company import company_resume
from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company_resources import CompanyDocumentFilesFixture, SQLiteCompanyEvidenceIndexFixture
from tests.helpers.download.company_resources import SQLiteCompanyFactsIndexFixture
from tests.helpers.download.company.company_startup import _snapshot
from trader.download.domain.company_facts import CompileCompanyFacts
from trader.download.domain.company_evidence import CompanyListing, DisclosurePage, DisclosurePartition
from trader.download.domain.company_field_plan import BuildCompanyFieldResults
from trader.download.domain.company_field_results import CompanyFieldConclusion
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_field_result_rows import WriteCompanyFieldResults
from trader.download.infra.company_facts_protocol import CompanyExtractionHash, EncodeCompanyDate, EncodeCompanyTime
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.download.infra.company_facts_schema import COMPANY_FACTS_SCHEMA
from trader.infra.sqlite_connection import EnsureSqliteSchema, SqliteConnectionOptions, SqliteSession
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.infra.sqlite_publication import PublishSqliteFile


_AT = fixtures.NOW
_IO = SqliteIoPolicy(1.0, 128, 128, "PASSIVE")
_KEY = "replay-material"
_TEXT = (
    "证券代码：600001\n本公告披露本次回购的实际执行情况，相关信息请以正式公告为准。\n"
    "本公司首次回购股份100万股，占公司总股本的比例为0.3%。\n"
    "回购股份用于注销。\n本公告仅说明本次事项的实际执行情况。相关安排已经正式披露，"
    "公司将根据实际执行情况及时披露后续进度。"
)


def _sources(root):
    incoming = root / "input"
    incoming.mkdir()
    raw = incoming / "raw" / "company_collection.sqlite3"
    scratch = incoming / "scratch.sqlite3"
    policy = fixtures.CompanyEventPolicy()
    index = SQLiteCompanyEvidenceIndexFixture(
        raw, facts_path=scratch, documents=CompanyDocumentFilesFixture(raw.parent / "text"),
        listings=(CompanyListing("600001", _AT.date() - timedelta(days=365)),),
    )
    index.initialize()
    item = fixtures._disclosure(_KEY, "关于首次回购公司股份的公告", digest="replay-record")
    index.commit_page(
        DisclosurePartition("sse", _AT.date(), _AT.date()), DisclosurePage((item,), 1, False), _AT,
    )
    extraction = fixtures._extraction(_TEXT)
    assert extraction.text == _TEXT
    assert index.commit_document(item, extraction, _AT)
    with SqliteSession(raw, sqlite_policy=_IO) as collected:
        collected.run("INSERT INTO company_population VALUES('600001')")
    # Both sources use the current structure; replay never upgrades a database.
    source = incoming / "company_facts.sqlite3"
    at = EncodeCompanyTime(_AT)
    with SqliteSession(source) as connection:
        EnsureSqliteSchema(connection, COMPANY_FACTS_SCHEMA, allow_create=True)
        connection.run(
            "INSERT INTO company_facts(code,finance_state,pledge_state,unlock_state,pending_docs,announcement_state,"
            "announcement_start_date,announcement_end_date,checked_at,published_at) VALUES('600001',0,0,0,0,1,?,?,?,?)",
            (EncodeCompanyDate(_AT.date() - timedelta(days=365)), EncodeCompanyDate(_AT.date()), at, at),
        )
        connection.run(
            "INSERT INTO company_positive_event(code,event_key,event_type,subject_type,status,first_disclosed_at,"
            "last_disclosed_at,level,actual_pct,repurchase_purpose) VALUES('600001',?,1,1,2,?,?,0,0.3,1)",
            (f"{_KEY}:share_repurchase", at, at),
        )
        connection.run("INSERT INTO company_evidence_ref VALUES(17,1,?)", (_KEY,))
        positive_id = connection.run("SELECT id FROM company_positive_event").require_one()[0]
        connection.run("INSERT INTO company_positive_evidence VALUES(?,17,0)", (positive_id,))
        connection.run(
            "INSERT INTO company_input_receipt VALUES(?,?,?,?,?,?,?,?,?)",
            ("cninfo", _KEY, "600001", item.digest, CompanyExtractionHash(policy),
             hashlib.sha256(_TEXT.encode()).digest(), at, "valid", "legacy-input-policy"),
        )
        connection.run("INSERT INTO company_publication VALUES(1,?,?,?)", (CompanyExtractionHash(policy), b"s" * 32, at))
        for fact in ReadCompanyFactRows(connection, ("600001",)):
            WriteCompanyFieldResults(connection, fact.code, tuple(
                CompanyFieldConclusion(item.code, item.event_id, item.field, item.value,
                                       item.state, item.applicable, item.reason, ())
                for item in BuildCompanyFieldResults(fact)
            ))
    PublishSqliteFile(source, sqlite_policy=_IO)
    return raw, source, policy


def _run(raw, facts, directory, policy, *, keys=(_KEY,), protected_roots=(), recovered_text=None):
    return replay.AuditCompanyFieldReplay(
        raw, facts, keys, directory, observed_at=_AT, budget_seconds=30,
        sqlite_policy=_IO, event_policy=policy, protected_roots=protected_roots,
        recovered_text=recovered_text,
    )


def _no_network(*_args, **_kwargs):
    raise AssertionError("local replay must not create a network connection")


def test_worker_replays_at_explicit_snapshot_time_without_rewriting_source_coverage(tmp_path, monkeypatch, capsys):
    raw, facts, policy = _sources(tmp_path)
    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr("sys.argv", [
        "company_resume", "--database", str(raw), "--facts-database", str(facts),
        "--field-replay-keys", _KEY, "--directory", str(tmp_path / "historical"),
        "--budget-seconds", "30", "--replay-observed-at", _AT.isoformat(),
    ])
    with SqliteSession(raw, SqliteConnectionOptions(read_only=True), sqlite_policy=_IO) as existing_reader:
        existing_reader.run("SELECT COUNT(*) FROM disclosures").require_one()
        assert company_resume.Main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "passed", report
    assert report["observed_at"] == _AT.isoformat()
    assert report["source_input_unchanged"] and report["second_zero_publication_text_reads"]
    assert report["full_market_verified"] is False and report["production_authorized"] is False
    incompatible = replay.AuditCompanyFieldReplay(
        raw, facts, (_KEY,), tmp_path / "wrong-day", observed_at=_AT + timedelta(days=1),
        budget_seconds=30, sqlite_policy=_IO, event_policy=policy, protected_roots=(),
    )
    assert incompatible["status"] == "failed"
    assert incompatible["failure_code"] == "company_field_replay_listing_context_missing"


def test_worker_rejects_observation_outside_field_replay_before_opening_database(tmp_path, monkeypatch):
    raw = tmp_path / "absent.sqlite3"
    monkeypatch.setattr("sys.argv", [
        "company_resume", "--database", str(raw), "--database-only", "--budget-seconds", "30",
        "--replay-observed-at", _AT.isoformat(),
    ])
    with pytest.raises(SystemExit):
        company_resume.Main()
    assert not raw.exists()


def test_real_publication_proves_local_fields_and_second_restart_reuses_them(tmp_path, monkeypatch):
    raw, facts, policy = _sources(tmp_path)
    before = raw.read_bytes(), facts.read_bytes(), Path(f"{facts}.sha256").read_bytes()
    monkeypatch.setattr(socket, "create_connection", _no_network)
    directory = tmp_path / "replay"
    # An existing reader is a normal live-collection lifecycle. Its WAL
    # sidecars already exist before the audit pins source file fingerprints.
    with SqliteSession(raw, SqliteConnectionOptions(read_only=True), sqlite_policy=_IO) as existing_reader:
        existing_reader.run("SELECT COUNT(*) FROM disclosures").require_one()
        report = _run(raw, facts, directory, policy)

    assert report["status"] == "passed", report
    assert report["schema_version"] == "company-field-replay"
    assert report["facts_backup_generation_stable"] and report["source_input_unchanged"]
    assert report["network_requests"] == report["source_data_writes"] == 0
    assert not report["production_authorized"] and not report["field_correctness_verified"]
    assert report["copied_texts"] == 1 and report["missing_texts"] == 0
    assert report["field_states"] == {"verified": 2}
    assert report["round_1_publication_text_reads"] > 0
    assert report["round_2_publication_text_reads"] == 0 and report["second_zero_publication_text_reads"]
    assert report["second_new_field_attempted"] == 0 and report["first_field_attempted"] > 0
    assert report["second_fields_and_publication_unchanged"] and report["original_valid_receipts_unchanged"]
    assert report["original_risks_and_evidence_preserved"] and report["unselected_facts_unchanged"]
    assert report["round_1_risk_comparison"]["all_old_risks_and_evidence_preserved"]
    assert report["round_2_risk_comparison"]["all_old_risks_and_evidence_preserved"]
    assert sum(item["reads"] for item in report["round_1_publication_material_reads"]) == report["round_1_publication_text_reads"]
    assert report["round_2_publication_material_reads"] == []
    assert report["first_publication_hash"] == report["second_publication_hash"]
    assert (raw.read_bytes(), facts.read_bytes(), Path(f"{facts}.sha256").read_bytes()) == before
    target = directory / "facts" / "company_facts.sqlite3"
    reader = SQLiteCompanyEvidenceReader(target, event_policy=policy, sqlite_policy=_IO)
    fields = reader.read_fields(("600001",)).results
    assert {item.field for item in fields} == {"actual_pct", "repurchase_purpose"}
    assert all(item.state == "verified" for item in fields)
    actual = next(item for item in fields if item.field == "actual_pct")
    assert actual.value == 0.3 and actual.evidence[0].body_hash == hashlib.sha256(_TEXT.encode()).digest()
    assert (actual.evidence[0].start_line, actual.evidence[0].end_line) == (3, 3)
    saved = json.loads((directory / "field-replay.json").read_text(encoding="utf-8"))
    assert saved["field_items"] and all("quote" not in proof for item in saved["field_items"] for proof in item["evidence"])
    assert _TEXT not in (directory / "field-replay.json").read_text(encoding="utf-8")


def test_missing_local_material_is_counted_and_never_downloaded(tmp_path, monkeypatch):
    raw, facts, policy = _sources(tmp_path)
    for path in (raw.parent / "text").glob("*/*.txt.gz"):
        path.unlink()
    monkeypatch.setattr(socket, "create_connection", _no_network)
    report = _run(raw, facts, tmp_path / "replay", policy)
    assert report["status"] == "degraded", report
    assert report["missing_texts"] == 1 and report["copied_texts"] == 0
    assert report["initial_missing_texts"] == 1 and report["final_required_texts"] >= 1
    assert report["network_requests"] == 0 and report["source_data_writes"] == 0
    assert report["field_states"] == {"present_unverified": 2}
    assert report["first_field_attempted"] == 0 and report["second_new_field_attempted"] == 0


def test_missing_unrequired_body_is_recounted_after_normal_material_cleanup(tmp_path):
    raw, facts, policy = _sources(tmp_path)
    with SqliteSession(raw, sqlite_policy=_IO) as connection:
        connection.run(
            "INSERT INTO disclosures(source,source_key,kind,title,published_at,url,record_hash,body_hash,body_checked,body_retained) "
            "VALUES('cninfo','ordinary','announcement','普通事项公告',?,'','ordinary',?,1,1)",
            (_AT.isoformat(), b"m" * 32),
        )
        connection.run("INSERT INTO disclosure_codes SELECT '600001',id FROM disclosures WHERE source_key='ordinary'")
    with SqliteSession(raw, SqliteConnectionOptions(read_only=True), sqlite_policy=_IO) as existing_reader:
        existing_reader.run("SELECT COUNT(*) FROM disclosures").require_one()
        report = _run(raw, facts, tmp_path / "replay", policy)
    assert report["initial_missing_texts"] == 1 and report["missing_texts"] == 0
    assert report["final_required_texts"] < report["required_texts"]
    assert report["original_valid_receipts_unchanged"] and report["status"] == "passed", report


@pytest.mark.parametrize("mutation", (None, "unselected_financial", "risk", "risk_evidence"))
def test_global_old_risks_and_unselected_facts_are_checked_independently_of_valid_receipts(tmp_path, monkeypatch, mutation):
    raw, facts, policy = _sources(tmp_path)
    old_risk = CompileCompanyFacts(_snapshot(), policy)
    with SqliteSession(facts, sqlite_policy=_IO) as connection:
        parent = ReadCompanyPublicationIdentity(connection)
        current, = ReadCompanyFactRows(connection, ("600001",))
    code = "600002" if mutation in (None, "unselected_financial") else "600001"
    retained = replace(
        old_risk, code=code,
        financial=replace(old_risk.financial, code=code) if old_risk.financial else None,
        latest_financial=replace(old_risk.latest_financial, code=code) if old_risk.latest_financial else None,
        pledge=replace(old_risk.pledge, code=code) if old_risk.pledge else None,
        unlock_schedule=tuple(replace(item, code=code) for item in old_risk.unlock_schedule),
    ) if code == "600002" else replace(current, risk_events=old_risk.risk_events)
    writer = SQLiteCompanyFactsIndexFixture(facts, event_policy=policy)
    writer.publish((retained,), {_KEY: "cninfo", "investigation": "cninfo"}, parent, _AT)
    PublishSqliteFile(facts, sqlite_policy=_IO)
    original_publish = replay.SQLiteCompanyEvidenceIndex.publish

    def publish(index, *args, **kwargs):
        result = original_publish(index, *args, **kwargs)
        if mutation is not None and not kwargs.get("changed_only", False):
            with SqliteSession(index.facts_path, SqliteConnectionOptions(foreign_keys=True), sqlite_policy=_IO) as connection:
                if mutation == "unselected_financial":
                    connection.run("UPDATE company_facts SET eps=eps+1 WHERE code='600002'")
                elif mutation == "risk":
                    connection.run("DELETE FROM company_risk_event")
                else:
                    connection.run("DELETE FROM company_risk_evidence")
        return result

    monkeypatch.setattr(replay.SQLiteCompanyEvidenceIndex, "publish", publish)
    with SqliteSession(raw, SqliteConnectionOptions(read_only=True), sqlite_policy=_IO) as existing_reader:
        existing_reader.run("SELECT COUNT(*) FROM disclosures").require_one()
        report = _run(raw, facts, tmp_path / "replay", policy)
    if mutation is None:
        assert report["status"] == "passed" and report["unselected_stocks"] == 1, report
        assert report["unselected_facts_unchanged"] and report["original_risks_and_evidence_preserved"]
        assert report["round_1_risk_comparison"]["exactly_preserved"] == 1
    else:
        assert report["status"] != "passed", report
        if mutation == "unselected_financial":
            assert not report["unselected_facts_unchanged"]
        else:
            assert not report["round_1_risk_comparison"]["all_old_risks_and_evidence_preserved"]


def test_explicit_same_hash_text_is_recovered_only_in_the_copy_then_normally_published(tmp_path, monkeypatch):
    raw, facts, policy = _sources(tmp_path)
    for path in (raw.parent / "text").glob("*/*.txt.gz"):
        path.unlink()
    supplied = tmp_path / "recovered.txt"
    supplied.write_text(_TEXT, encoding="utf-8")
    before = raw.read_bytes(), facts.read_bytes(), Path(f"{facts}.sha256").read_bytes()
    monkeypatch.setattr(socket, "create_connection", _no_network)
    report = _run(raw, facts, tmp_path / "replay", policy, recovered_text=supplied)
    assert report["recovered_required_text"] is True
    assert report["initial_missing_texts"] == 1 and report["missing_texts"] == 0
    assert report["field_states"] == {"verified": 2}
    assert report["original_valid_receipts_unchanged"] and report["second_fields_and_publication_unchanged"]
    assert report["second_new_field_attempted"] == report["network_requests"] == report["source_data_writes"] == 0
    assert (raw.read_bytes(), facts.read_bytes(), Path(f"{facts}.sha256").read_bytes()) == before
    assert not tuple((raw.parent / "text").glob("*/*.txt.gz"))


@pytest.mark.parametrize("guard", ("text", "receipt", "revision"))
def test_recovered_text_requires_exact_current_hash_and_formal_receipt(tmp_path, guard):
    raw, facts, policy = _sources(tmp_path)
    for path in (raw.parent / "text").glob("*/*.txt.gz"):
        path.unlink()
    supplied = tmp_path / "recovered.txt"
    supplied.write_text(_TEXT + ("different" if guard == "text" else ""), encoding="utf-8")
    if guard == "receipt":
        with SqliteSession(facts, sqlite_policy=_IO) as connection:
            connection.run("DELETE FROM company_input_receipt")
        PublishSqliteFile(facts, sqlite_policy=_IO)
    if guard == "revision":
        with SqliteSession(raw, sqlite_policy=_IO) as connection:
            connection.run("UPDATE disclosures SET record_hash='new-directory-version'")
    report = _run(raw, facts, tmp_path / "replay", policy, recovered_text=supplied)
    assert report["status"] == "failed"
    assert report["failure_code"] in {"company_material_recovery_identity_mismatch", "company_material_recovery_required_version_missing"}
    assert report["network_requests"] == report["source_data_writes"] == 0


def test_read_only_empty_wal_creation_remains_an_explicit_physical_change(tmp_path):
    raw, facts, policy = _sources(tmp_path)
    assert not Path(f"{raw}-wal").exists()
    report = _run(raw, facts, tmp_path / "replay", policy)
    assert report["status"] == "degraded" and report["source_empty_wal_created"]
    assert not report["source_input_unchanged"] and not report["collection_source_files_unchanged"]
    assert report["facts_source_files_unchanged"] and report["field_states"] == {"verified": 2}
    before, after = report["collection_source_files_sha256_before"], report["collection_source_files_sha256_after"]
    assert before[0] == after[0] and before[1] is None and after[1] == hashlib.sha256(b"").hexdigest()
    assert report["source_input_comparison"] == "database_wal_receipt_sha256_exact"
    assert report["source_data_writes"] == 0


def test_external_nonempty_wal_change_cannot_be_reported_unchanged(tmp_path, monkeypatch):
    raw, facts, policy = _sources(tmp_path)
    backup = replay._backup_collection
    # This writer represents the independent live producer, after the audit's
    # consistent collection snapshot. It is not a replay-owned write path.
    with SqliteSession(raw, sqlite_policy=_IO) as producer:
        producer.run("INSERT INTO component_checks VALUES('external','before',?,'')", (_AT.isoformat(),))

        def backup_then_external_commit(*args):
            result = backup(*args)
            producer.run("INSERT INTO component_checks VALUES('external','after',?,'')", (_AT.isoformat(),))
            return result

        monkeypatch.setattr(replay, "_backup_collection", backup_then_external_commit)
        report = _run(raw, facts, tmp_path / "replay", policy)
    assert report["status"] == "degraded" and not report["source_input_unchanged"]
    before, after = report["collection_source_files_sha256_before"], report["collection_source_files_sha256_after"]
    assert before[0] == after[0] and before[1] != after[1]
    assert before[1] not in {None, hashlib.sha256(b"").hexdigest()}
    assert not report["source_empty_wal_created"] and report["source_data_writes"] == 0
    assert report["field_states"] == {"verified": 2}


@pytest.mark.parametrize("keys", ((), (_KEY, _KEY), ("bad key",), ("k",) * 7, (42,), ([],)))
def test_invalid_keys_are_rejected_before_output_creation(tmp_path, keys):
    raw, facts, policy = _sources(tmp_path)
    destination = tmp_path / "replay"
    with pytest.raises(ValueError, match="keys_invalid"):
        _run(raw, facts, destination, policy, keys=keys)
    assert not destination.exists()


@pytest.mark.parametrize("boundary", ("source", "protected", "nonempty", "symlink", "relative"))
def test_output_boundaries_reject_overwrite_overlap_and_links(tmp_path, boundary):
    raw, facts, policy = _sources(tmp_path)
    destination = tmp_path / "replay"
    protected_roots = ()
    if boundary == "source":
        destination = raw.parent / "replay"
    elif boundary == "protected":
        protected_roots = (tmp_path / "protected",)
        destination = protected_roots[0] / "replay"
    elif boundary == "nonempty":
        destination.mkdir()
        (destination / "keep").write_text("keep", encoding="utf-8")
    elif boundary == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        destination.symlink_to(target, target_is_directory=True)
    else:
        destination = Path("relative-replay")
    before = sorted(str(path) for path in tmp_path.rglob("*"))
    with pytest.raises(ValueError):
        _run(raw, facts, destination, policy, protected_roots=protected_roots)
    assert sorted(str(path) for path in tmp_path.rglob("*")) == before


@pytest.mark.parametrize("problem", ("missing", "multiple_codes", "listing_context", "text_symlink"))
def test_source_context_and_text_path_must_be_unambiguous(tmp_path, problem):
    raw, facts, policy = _sources(tmp_path)
    keys = (_KEY,)
    if problem == "missing":
        keys = ("missing-key",)
    elif problem == "multiple_codes":
        with SqliteSession(raw) as connection:
            connection.run("INSERT INTO disclosure_codes SELECT '600002',id FROM disclosures")
    elif problem == "listing_context":
        with SqliteSession(facts) as connection:
            connection.run("UPDATE company_facts SET announcement_start_date=?", (EncodeCompanyDate(_AT.date()),))
        PublishSqliteFile(facts, sqlite_policy=_IO)
    else:
        text_path, = (raw.parent / "text").glob("*/*.txt.gz")
        outside = tmp_path / "untrusted.gz"
        text_path.rename(outside)
        text_path.symlink_to(outside)
    report = _run(raw, facts, tmp_path / "replay", policy, keys=keys)
    assert report["status"] == "failed", report
    assert report["failure_code"] in {
        "company_field_replay_selected_source_ambiguous", "company_field_replay_listing_context_missing",
        "company_field_replay_symlink_rejected",
    }
    assert report["network_requests"] == report["source_data_writes"] == 0


def test_incompatible_facts_are_rejected_without_conversion_or_replay(tmp_path):
    raw, facts, policy = _sources(tmp_path)
    with SqliteSession(facts) as connection:
        connection.run("DROP TABLE company_field_result")
    PublishSqliteFile(facts, sqlite_policy=_IO)
    before = facts.read_bytes()
    report = _run(raw, facts, tmp_path / "replay", policy)
    assert report["status"] == "failed" and "facts_backup_generation_stable" not in report
    assert facts.read_bytes() == before
    assert not (tmp_path / "replay" / "raw").exists()
