"""The public company diagnostic isolates writes and retains bounded recovery evidence."""

import json
import sys
import time
from datetime import timedelta

import pytest

from scripts.diagnose_runtime import DiagnosticResult, _parser, _validate, build_commands, build_report
from scripts.runtime_diagnostics import company_pagination_audit, company_pipeline_audit
from scripts.runtime_diagnostics.company_query_audit import AuditCompanyStorage
from scripts.runtime_diagnostics.company_resume import Main, _compact_storage, _copy, _saved_pages
from tests.component import test_company_evidence as fixtures
from tests.unit.infra.test_company_sources import NOW
from trader.download.domain.company_evidence import DisclosurePage, DisclosureQuery
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction


def test_pagination_probe_rejects_ignored_larger_page_sizes_without_writes(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    partition = index.partitions(("sse",), fixtures.NOW)[0]
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run(
            "UPDATE partitions SET start=end,expected_rows=31,complete=1 WHERE market=? AND start=? AND end=?",
            (partition.market, str(partition.start), str(partition.end)),
        )
    items = tuple(fixtures._disclosure(str(i)) for i in range(31))

    class Source:
        def __init__(self, *args, **kwargs):
            pass

        def fetch(self, query):
            return DisclosurePage(items[:30] if query.page == 1 else items[30:], 31, query.page == 1)

    monkeypatch.setattr(company_pagination_audit, "CninfoMarketDisclosureSource", Source)
    before = index.path.read_bytes()
    result = company_pagination_audit.AuditCompanyPagination(index.path, budget_seconds=5)
    rows = result["summary"]["query_audit"]["pagination"]
    assert [row["equivalent"] for row in rows] == [True, False, False]
    assert all(row["failure"] == "cninfo_requested_page_size_ignored" for row in rows[1:])
    assert index.path.read_bytes() == before


def test_pipeline_audit_requires_external_directory_and_routes_to_its_owner(tmp_path):
    with pytest.raises(ValueError, match="company-replay-directory"):
        _validate(_parser().parse_args(["--profile", "company-resume", "--company-pipeline-audit"]))
    options, _ = _validate(
        _parser().parse_args(
            ["--profile", "company-resume", "--company-pipeline-audit", "--company-replay-directory", str(tmp_path)]
        )
    )
    assert "--pipeline-only" in build_commands(options)[0].argv


def test_pipeline_probe_compares_facts_without_observation_time_noise(tmp_path, monkeypatch):
    now = company_pipeline_audit.datetime.now(fixtures.NOW.tzinfo)
    items = tuple(fixtures._disclosure(str(i), "本公司收到立案告知书", when=now) for i in range(2))
    monkeypatch.setattr(company_pipeline_audit, "_sample", lambda _: items)

    class Source:
        def __init__(self, *args, **kwargs):
            pass

        def document(self, item):
            return "本公司收到中国证监会立案告知书。"

    monkeypatch.setattr(company_pipeline_audit, "CninfoMarketDisclosureSource", Source)
    result = company_pipeline_audit.AuditCompanyPipeline(tmp_path / "unused.sqlite3", tmp_path, budget_seconds=30)
    assert result["status"] == "passed"
    assert result["summary"]["pipeline_audit"]["equivalent_facts"]
    for run in result["summary"]["pipeline_audit"]["runs"]:
        assert run["attempts"] == 2 and run["resume_attempts"] == run["resume_changed_rows"] == 0


def test_company_profile_requires_explicit_external_directory(tmp_path):
    with pytest.raises(ValueError, match="company-replay-directory"):
        _validate(_parser().parse_args(["--profile", "company-resume"]))
    with pytest.raises(ValueError, match="outside the repository"):
        _validate(_parser().parse_args(["--profile", "company-resume", "--company-replay-directory", "data/replay"]))
    options, _ = _validate(
        _parser().parse_args(["--profile", "company-resume", "--company-replay-directory", str(tmp_path)])
    )
    commands = build_commands(options)
    assert len(commands) == 1 and commands[0].name == "company_resume"
    assert str(tmp_path) in commands[0].argv
    audit, _ = _validate(
        _parser().parse_args(
            [
                "--profile",
                "company-resume",
                "--company-replay-directory",
                str(tmp_path),
                "--company-query-audit",
            ]
        )
    )
    assert "--audit-only" in build_commands(audit)[0].argv


def test_storage_audit_needs_no_replay_directory_or_network_and_keeps_database_unchanged(tmp_path, monkeypatch, capsys):
    index = fixtures._index(tmp_path)
    partition = index.partitions(("sse",), fixtures.NOW)[0]
    item = fixtures._disclosure(title="收到立案告知书")
    index.commit_page(partition, DisclosurePage((item,), 1, False), fixtures.NOW)
    index.document_failure(
        item, "fixture", "disclosure_document_no_text", fixtures.NOW, fixtures.NOW + timedelta(days=1)
    )
    index.publish(("600001",), fixtures.NOW)
    before = index.path.read_bytes()
    report = AuditCompanyStorage(index.path, now=fixtures.NOW, budget_seconds=5)
    storage = report["summary"]["local_storage"]
    assert storage["announcement_records"] == storage["pending_documents"] == storage["stored_retry_cooling"] == 1
    assert storage["extracted_bodies"] == 0 and not storage["pdf_archived"]
    assert storage["free_bytes"] >= 0
    assert any(row["name"] == "disclosures" and row["bytes"] > 0 for row in storage["table_bytes"])
    assert storage["document_failures"] == [{"failure": "disclosure_document_no_text", "documents": 1}]
    options, _ = _validate(_parser().parse_args(["--profile", "company-resume", "--company-storage-audit"]))
    command = build_commands(options)[0]
    assert "--storage-only" in command.argv and "--directory" not in command.argv
    monkeypatch.setattr(
        sys, "argv", ["company-resume", "--database", str(index.path), "--storage-only", "--budget-seconds", "30"]
    )
    assert Main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["summary"]["local_storage"]["read_only"] and index.path.read_bytes() == before


def test_backup_preserves_source_and_is_independently_writable(tmp_path):
    source, replica = tmp_path / "source.sqlite3", tmp_path / "replica.sqlite3"
    with SqliteSession(source) as connection, SqliteTransaction(connection):
        connection.run("CREATE TABLE facts(value INTEGER)")
        connection.run("INSERT INTO facts VALUES(7)")
    before = source.read_bytes()
    # A stale intermediate name must never let the backup write through a source hard link.
    stale = replica.with_suffix(".copying.sqlite3")
    stale.hardlink_to(source)
    _copy(source, replica, deadline=time.monotonic() + 5)
    with SqliteSession(replica) as connection, SqliteTransaction(connection):
        connection.run("UPDATE facts SET value=9")
    assert source.read_bytes() == before and stale.samefile(source)


def test_company_compaction_retains_failed_range_members_facts_bodies_and_retry_state(tmp_path):
    index = fixtures._index(tmp_path)
    complete, pending = index.partitions(("sse",), fixtures.NOW)[:2]
    index.commit_page(complete, DisclosurePage((fixtures._disclosure("done"),), 1, False), fixtures.NOW)
    index.commit_page(pending, DisclosurePage((fixtures._disclosure("pending"),), 2, True), fixtures.NOW)
    index.publish(("600001",), fixtures.NOW)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        # Model the redundant completed members retained by the previous release.
        connection.run(
            "INSERT INTO partition_rows VALUES(?,?,?,?,?)",
            (complete.market, str(complete.start), str(complete.end), "done", "600001"),
        )
    before = index.path.read_bytes()
    report = _compact_storage(index.path, tmp_path / "isolated", budget_seconds=10)
    assert report["removed_completed_members"] == 1 and report["retained_data_equal"]
    with SqliteSession(tmp_path / "isolated/company_compacted.sqlite3") as connection:
        assert connection.run("SELECT source_id FROM partition_rows").require_one()[0] == "pending"
    assert index.path.read_bytes() == before


def test_backup_includes_committed_wal_without_changing_the_source(tmp_path):
    source, replica = tmp_path / "source.sqlite3", tmp_path / "replica.sqlite3"
    with SqliteSession(source, SqliteConnectionOptions(journal_mode="WAL")) as writer:
        with SqliteTransaction(writer):
            writer.run("CREATE TABLE facts(value INTEGER)")
            writer.run("INSERT INTO facts VALUES(7)")
        wal = source.with_name(source.name + "-wal")
        before = (source.read_bytes(), wal.read_bytes())
        assert before[1]
        _copy(source, replica, deadline=time.monotonic() + 5)
        with SqliteSession(replica) as connection:
            assert connection.run("SELECT value FROM facts").require_one()[0] == 7
            connection.run("UPDATE facts SET value=9")
        assert writer.run("SELECT value FROM facts").require_one()[0] == 7
        assert (source.read_bytes(), wal.read_bytes()) == before


def test_backup_timeout_preserves_previous_replica_and_removes_partial_file(tmp_path):
    source, replica = tmp_path / "source.sqlite3", tmp_path / "replica.sqlite3"
    with SqliteSession(source) as connection:
        connection.run("CREATE TABLE facts(value INTEGER)")
    with SqliteSession(replica) as connection:
        connection.run("CREATE TABLE previous(value INTEGER)")
    before = replica.read_bytes()
    with pytest.raises(TimeoutError, match="sqlite_backup_deadline_exceeded"):
        _copy(source, replica, deadline=time.monotonic() - 1)
    assert replica.read_bytes() == before
    assert list(tmp_path.glob(".company-copy-*")) == []


def test_discovery_response_is_not_counted_as_a_saved_page_replayed_by_parent_verification(tmp_path):
    index = SQLiteCompanyEvidenceIndex(
        tmp_path / "evidence.sqlite3",
        facts_path=(tmp_path / "evidence.sqlite3").parent / "company_facts.sqlite3",
        documents=CompanyDocumentFiles((tmp_path / "evidence.sqlite3").parent / "text"),
    )
    index.initialize()
    root = index.partitions(("sse",), NOW)[0]
    child = index.split_query(root, (DisclosureQuery("shmb"), DisclosureQuery("shkcp")))[0]
    index.commit_page(child, DisclosurePage((), 0, False), NOW)
    parent_key = (root.market, str(root.start), str(root.end), "", 1)
    child_key = (child.market, str(child.start), str(child.end), child.query.key, 1)
    assert _saved_pages(index.path, [parent_key, child_key]) == {child_key}


def test_replay_rejects_hard_link_to_active_database_before_any_write(tmp_path, monkeypatch):
    source = tmp_path / "active.sqlite3"
    with SqliteSession(source) as connection:
        connection.run("CREATE TABLE facts(value INTEGER)")
    directory = tmp_path / "replay"
    directory.mkdir()
    (directory / "evidence.sqlite3").hardlink_to(source)
    before = source.read_bytes()
    monkeypatch.setattr(
        sys,
        "argv",
        ["company-resume", "--database", str(source), "--directory", str(directory), "--budget-seconds", "60"],
    )
    with pytest.raises(SystemExit) as exc:
        Main()
    assert exc.value.code == 2 and source.read_bytes() == before


def test_recovery_report_keeps_counts_and_omits_unrequested_payload():
    result = DiagnosticResult(
        "company_resume",
        0,
        25.0,
        {
            "schema_version": "company-resume",
            "status": "passed",
            "summary": {
                "selected_slices": 6,
                "completed_slices": 6,
                "covered_slices": 6,
                "saved_pages_replayed": 0,
                "private_database": "must-not-project",
                "runs": [{"pages": 100, "document_attempts": 32, "raw_document": "must-not-project"}],
                "local_storage": {
                    "read_only": True,
                    "extracted_bodies": 2,
                    "retained_bodies": 1,
                    "body_characters": 999,
                    "published_stocks": 999,
                    "formal": {
                        "available": True,
                        "database_bytes": 4096,
                        "stocks": 2,
                        "table_bytes": [{"name": "company_facts", "bytes": 4096, "secret": "must-not-project"}],
                        "raw_facts": "must-not-project",
                    },
                    "body": "must-not-project",
                    "document_failures": [{"failure": "no_text", "documents": 1, "private": "must-not-project"}],
                },
                "query_audit": {
                    "read_only": True,
                    "pages": [{"total": 93, "outside_codes": 2, "same_issuer": 2, "raw_response": "must-not-project"}],
                    "totals": [{"parent_total": 100, "group_total": 102, "private": "must-not-project"}],
                    "private": "must-not-project",
                },
            },
        },
        None,
    )
    report = build_report("company-resume", [result])
    text = json.dumps(report)
    assert "must-not-project" not in text
    summary = report["checks"][0]["summary"]
    assert summary["selected_slices"] == summary["completed_slices"] == 6
    assert summary["runs"][0]["pages"] == 100 and summary["saved_pages_replayed"] == 0
    assert summary["query_audit"]["pages"][0]["same_issuer"] == 2
    assert summary["local_storage"]["extracted_bodies"] == 2
    assert summary["local_storage"]["retained_bodies"] == 1
    assert "body_characters" not in summary["local_storage"]
    assert "published_stocks" not in summary["local_storage"]
    assert summary["local_storage"]["formal"] == {
        "available": True,
        "database_bytes": 4096,
        "stocks": 2,
        "table_bytes": [{"name": "company_facts", "bytes": 4096}],
    }
    assert summary["local_storage"]["document_failures"] == [{"failure": "no_text", "documents": 1}]


def test_query_capture_distinguishes_an_issuer_alias_from_an_unrelated_response():
    from scripts.runtime_diagnostics.company_query_audit import _Capture
    from trader.infra.http_contracts import HttpRequest, HttpResponse

    capture = _Capture()
    request = HttpRequest(
        "POST", "https://www.cninfo.com.cn/new/hisAnnouncement/query", 8, body=b"stock=600001%2Cissuer"
    )
    response = HttpResponse(
        200,
        json.dumps(
            {
                "totalRecordNum": 2,
                "announcements": [
                    {"secCode": "600002", "orgId": "issuer"},
                    {"secCode": "600003", "orgId": "unrelated"},
                ],
            }
        ).encode(),
    )
    capture.inspect(request, response)
    assert capture.total == 2 and capture.outside_codes == 2 and capture.same_issuer == 1
