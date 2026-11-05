"""The public company diagnostic isolates writes and retains bounded recovery evidence."""

from scripts.checks.catalog import SelectionArguments

from tests.helpers.configuration import SqlitePolicyFixture

import json
import sys
import time
from dataclasses import replace
from datetime import timedelta

import pytest

from scripts.checks.contracts import DiagnosticResult
from scripts.checks.arguments import BuildParser
from scripts.checks.arguments import ValidateOptions
from scripts.checks.commands import BuildCommands
from scripts.checks.projections import BuildReport
from scripts.checks.download.company import company_pagination_audit
from scripts.checks.download.company import company_pipeline_audit
from scripts.checks.download.company import company_ocr_audit
from scripts.checks.download.company.company_query_audit import AuditCompanyDatabase
from scripts.checks.download.company.company_resume import Main, _compact_database, _copy, _saved_pages
from tests.component import test_company_evidence as fixtures
from tests.unit.infra.test_company_sources import NOW
from trader.download.domain.company_evidence import CompanyDocumentFailure, DisclosurePage, DisclosureQuery
from tests.helpers.configuration import (
    CompanyDocumentFilesFixture as CompanyDocumentFiles,
    SQLiteCompanyEvidenceIndexFixture as SQLiteCompanyEvidenceIndex,
)
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction


def test_ocr_comparison_reuses_seven_identical_pdfs_without_writing_collection(tmp_path, monkeypatch):
    import hashlib
    from contextlib import nullcontext
    from tests.component.test_company_pipeline import _seed

    index = fixtures._index(tmp_path)
    items = _seed(index, 8)
    payload = b"%PDF-fixture"
    digest = hashlib.sha256(payload).digest()
    for item in items:
        index.document_failure(item, "policy", "disclosure_document_ocr_low_quality", fixtures.NOW,
                               fixtures.NOW + timedelta(days=1), digest)

    class Source:
        def __init__(self, *args, raw_files, **kwargs):
            self.raw = raw_files

        def download_document(self, item):
            directory = self.raw.directory(item)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / (digest.hex() + ".pdf")).write_bytes(payload)

    class Engine:
        def __init__(self, *args, dpi, **kwargs):
            self.dpi = dpi

        def extract(self, *args):
            if self.dpi == 150:
                raise CompanyDocumentFailure("disclosure_document_ocr_low_quality")
            return "合格正文" * 30, 80.0

    monkeypatch.setattr(company_ocr_audit, "OpenExternalHttp", lambda: nullcontext(None))
    monkeypatch.setattr(company_ocr_audit, "CninfoMarketDisclosureSource", Source)
    monkeypatch.setattr(company_ocr_audit, "CompanyOcrEngine", Engine)
    before = index.path.read_bytes()
    report = company_ocr_audit.AuditCompanyOcr(index.path, tmp_path / "probe", budget_seconds=30, sqlite_policy=SqlitePolicyFixture())
    result = report["summary"]["ocr_audit"]
    assert result["sample_size"] == 7 and not result["active_writes"]
    assert result["passed"] and result["recovered_documents"] == 7
    assert all(sample["same_pdf"] for sample in result["samples"])
    assert all([run["passed"] for run in sample["runs"]] == [False, False, True] for sample in result["samples"])
    assert index.path.read_bytes() == before
    assert len(tuple((tmp_path / "probe").rglob("*.pdf"))) == 7


def test_pagination_probe_rejects_ignored_larger_page_sizes_without_writes(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    partition = index.partitions(("sse",), fixtures.NOW)[0]
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run(
            "UPDATE partitions SET start_date=end_date,expected_rows=31,is_complete=1 WHERE market=? AND start_date=? AND end_date=?",
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
    result = company_pagination_audit.AuditCompanyPagination(index.path, budget_seconds=5, sqlite_policy=SqlitePolicyFixture())
    rows = result["summary"]["query_audit"]["pagination"]
    assert [row["equivalent"] for row in rows] == [True, False, False]
    assert all(row["failure"] == "cninfo_requested_page_size_ignored" for row in rows[1:])
    assert index.path.read_bytes() == before


@pytest.mark.parametrize("mode", ("pipeline", "ocr"))
def test_pipeline_audit_requires_external_directory_and_routes_to_its_owner(tmp_path, mode):
    with pytest.raises(ValueError, match="company-replay-directory"):
        ValidateOptions(BuildParser().parse_args([*SelectionArguments('company-resume'), f"--company-{mode}-audit"]))
    options, _ = ValidateOptions(
        BuildParser().parse_args(
            [*SelectionArguments('company-resume'), f"--company-{mode}-audit", "--company-replay-directory", str(tmp_path)]
        )
    )
    assert f"--{mode}-only" in BuildCommands(options)[0].argv


def test_pipeline_probe_compares_facts_without_observation_time_noise(tmp_path, monkeypatch):
    now = company_pipeline_audit.datetime.now(fixtures.NOW.tzinfo)
    items = tuple(fixtures._disclosure(str(i), "本公司收到立案告知书", when=now) for i in range(2))
    monkeypatch.setattr(company_pipeline_audit, "_sample", lambda _, *, sqlite_policy: items)

    class Source(fixtures.Sources):
        def __init__(self, *args, **kwargs):
            pass

        def document(self, item):
            return fixtures._extraction("本公司收到中国证监会立案告知书。")

    monkeypatch.setattr(company_pipeline_audit, "CninfoMarketDisclosureSource", Source)
    result = company_pipeline_audit.AuditCompanyPipeline(tmp_path / "unused.sqlite3", tmp_path, budget_seconds=30, sqlite_policy=SqlitePolicyFixture())
    assert result["status"] == "passed"
    assert result["summary"]["pipeline_audit"]["equivalent_facts"]
    assert result["summary"]["pipeline_audit"]["equivalent_documents"]
    assert result["summary"]["pipeline_audit"]["equivalent_payloads"]
    for run in result["summary"]["pipeline_audit"]["runs"]:
        assert run["attempts"] == 2 and run["resume_attempts"] == run["resume_changed_rows"] == 0
        assert run["extracted_documents"] == 2
        assert run["document_extractions"] == [{"method": "text", "documents": 2}]
        assert run["pending_ocr_documents"] == 0


def test_pipeline_rejects_changed_pdf_even_when_extracted_text_and_facts_match(tmp_path, monkeypatch):
    now = company_pipeline_audit.datetime.now(fixtures.NOW.tzinfo)
    monkeypatch.setattr(company_pipeline_audit, "_sample", lambda _, *, sqlite_policy: (fixtures._disclosure("changed", "收到立案告知书", when=now),))
    clients = []

    class Source(fixtures.Sources):
        def __init__(self, *args, **kwargs):
            self.digest = b"a" * 32 if not clients else b"b" * 32
            clients.append(self)

        def document(self, item):
            return replace(fixtures._extraction("本公司收到中国证监会立案告知书。"), payload_hash=self.digest)

    monkeypatch.setattr(company_pipeline_audit, "CninfoMarketDisclosureSource", Source)
    report = company_pipeline_audit.AuditCompanyPipeline(tmp_path / "unused.sqlite3", tmp_path, budget_seconds=30, sqlite_policy=SqlitePolicyFixture())
    summary = report["summary"]["pipeline_audit"]
    assert summary["equivalent_facts"] and summary["equivalent_documents"]
    assert not summary["equivalent_payloads"] and report["status"] == "degraded"
    assert all(run["extracted_documents"] == 1 for run in summary["runs"])


def test_company_profile_requires_explicit_external_directory(tmp_path):
    with pytest.raises(ValueError, match="company-replay-directory"):
        ValidateOptions(BuildParser().parse_args([*SelectionArguments('company-resume')]))
    with pytest.raises(ValueError, match="outside the repository"):
        ValidateOptions(BuildParser().parse_args([*SelectionArguments('company-resume'), "--company-replay-directory", "data/replay"]))
    options, _ = ValidateOptions(
        BuildParser().parse_args([*SelectionArguments('company-resume'), "--company-replay-directory", str(tmp_path)])
    )
    commands = BuildCommands(options)
    assert len(commands) == 1 and commands[0].name == "company_resume"
    assert str(tmp_path) in commands[0].argv
    audit, _ = ValidateOptions(
        BuildParser().parse_args(
            [
                *SelectionArguments('company-resume'),
                "--company-replay-directory",
                str(tmp_path),
                "--company-query-audit",
            ]
        )
    )
    assert "--audit-only" in BuildCommands(audit)[0].argv


def test_pipeline_probe_does_not_pass_when_both_runs_have_only_deterministic_failures(tmp_path, monkeypatch):
    now = company_pipeline_audit.datetime.now(fixtures.NOW.tzinfo)
    monkeypatch.setattr(company_pipeline_audit, "_sample", lambda _, *, sqlite_policy: (fixtures._disclosure("failure", "收到立案告知书", when=now),))
    capabilities = []

    class Source(fixtures.Sources):
        def __init__(self, *args, **kwargs):
            capabilities.append((kwargs["ocr_engine"], kwargs["text_capability"], kwargs["document_policies"]))

        def document(self, item):
            raise CompanyDocumentFailure("disclosure_document_ocr_low_quality", payload_hash=b"x" * 32)

    monkeypatch.setattr(company_pipeline_audit, "CninfoMarketDisclosureSource", Source)
    report = company_pipeline_audit.AuditCompanyPipeline(tmp_path / "unused.sqlite3", tmp_path, budget_seconds=30, sqlite_policy=SqlitePolicyFixture())
    assert capabilities and all(all(item is not None for item in row) for row in capabilities)
    assert report["status"] == "degraded"
    assert report["summary"]["pipeline_audit"]["equivalent_facts"]
    for run in report["summary"]["pipeline_audit"]["runs"]:
        assert run["pending_documents"] == run["resume_attempts"] == run["extracted_documents"] == 0
        assert run["document_failures"] == [{"failure": "disclosure_document_ocr_low_quality", "documents": 1}]


def test_database_audit_needs_no_replay_directory_or_network_and_keeps_database_unchanged(tmp_path, monkeypatch, capsys):
    index = fixtures._index(tmp_path)
    partition = index.partitions(("sse",), fixtures.NOW)[0]
    item = fixtures._disclosure(title="收到立案告知书")
    index.commit_page(partition, DisclosurePage((item,), 1, False), fixtures.NOW)
    index.document_failure(
        item, "fixture", "disclosure_document_no_text", fixtures.NOW, fixtures.NOW + timedelta(days=1)
    )
    index.publish(("600001",), fixtures.NOW)
    before = index.path.read_bytes()
    report = AuditCompanyDatabase(index.path, now=fixtures.NOW, budget_seconds=5, sqlite_policy=SqlitePolicyFixture())
    database = report["summary"]["database"]
    assert database["announcement_records"] == database["pending_documents"] == database["deterministic_failures"] == 1
    assert database["recorded_retry_cooling"] == database["recorded_retry_ready"] == 0
    assert database["extracted_bodies"] == 0 and not database["pdf_archived"]
    assert database["free_bytes"] >= 0
    assert any(row["name"] == "disclosures" and row["bytes"] > 0 for row in database["table_bytes"])
    assert database["document_failures"] == [{"failure": "disclosure_document_no_text", "documents": 1}]
    recent = database["recent_document_failures"]
    assert len(recent) == 1 and recent[0]["disclosure_id"] > 0
    assert recent[0]["failure"] == "disclosure_document_no_text"
    assert recent[0]["attempted_at"] == fixtures.NOW.isoformat()
    options, _ = ValidateOptions(BuildParser().parse_args([*SelectionArguments('company-resume'), "--company-database-audit"]))
    command = BuildCommands(options)[0]
    assert "--database-only" in command.argv and "--directory" not in command.argv
    monkeypatch.setattr(
        sys, "argv", ["company-resume", "--database", str(index.path), "--database-only", "--budget-seconds", "30"]
    )
    assert Main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["summary"]["database"]["read_only"] and index.path.read_bytes() == before


def test_backup_preserves_source_and_is_independently_writable(tmp_path):
    source, replica = tmp_path / "source.sqlite3", tmp_path / "replica.sqlite3"
    with SqliteSession(source) as connection, SqliteTransaction(connection):
        connection.run("CREATE TABLE facts(value INTEGER)")
        connection.run("INSERT INTO facts VALUES(7)")
    before = source.read_bytes()
    # A stale intermediate name must never let the backup write through a source hard link.
    stale = replica.with_suffix(".copying.sqlite3")
    stale.hardlink_to(source)
    _copy(source, replica, deadline=time.monotonic() + 5, sqlite_policy=SqlitePolicyFixture())
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
    report = _compact_database(index.path, tmp_path / "isolated", budget_seconds=10, sqlite_policy=SqlitePolicyFixture())
    assert report["removed_completed_members"] == 1 and report["retained_data_equal"]
    with SqliteSession(tmp_path / "isolated/company_compacted.sqlite3") as connection:
        assert connection.run("SELECT source_key FROM partition_rows").require_one()[0] == "pending"
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
        _copy(source, replica, deadline=time.monotonic() + 5, sqlite_policy=SqlitePolicyFixture())
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
        _copy(source, replica, deadline=time.monotonic() - 1, sqlite_policy=SqlitePolicyFixture())
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
    assert _saved_pages(index.path, [parent_key, child_key], sqlite_policy=SqlitePolicyFixture()) == {child_key}


def test_replay_rejects_hard_link_to_source_database_before_any_write(tmp_path, monkeypatch):
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
                "database": {
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
                    "pages": [{"total": 93, "outside_codes": 2, "same_issuer": 2, "code_prefixes": ["302"], "raw_response": "must-not-project"}],
                    "totals": [{"parent_total": 100, "group_total": 102, "private": "must-not-project"}],
                    "private": "must-not-project",
                },
            },
        },
        None,
    )
    report = BuildReport("company-resume", [result])
    text = json.dumps(report)
    assert "must-not-project" not in text
    summary = report["checks"][0]["summary"]
    assert summary["selected_slices"] == summary["completed_slices"] == 6
    assert summary["runs"][0]["pages"] == 100 and summary["saved_pages_replayed"] == 0
    assert summary["query_audit"]["pages"][0]["same_issuer"] == 2
    assert summary["query_audit"]["pages"][0]["code_prefixes"] == ["302"]
    assert summary["database"]["extracted_bodies"] == 2
    assert summary["database"]["retained_bodies"] == 1
    assert "body_characters" not in summary["database"]
    assert "published_stocks" not in summary["database"]
    assert summary["database"]["formal"] == {
        "available": True,
        "database_bytes": 4096,
        "stocks": 2,
        "table_bytes": [{"name": "company_facts", "bytes": 4096}],
    }
    assert summary["database"]["document_failures"] == [{"failure": "no_text", "documents": 1}]


def test_query_capture_distinguishes_an_issuer_alias_from_an_unrelated_response():
    from scripts.checks.download.company.company_query_audit import _Capture
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
    assert capture.code_prefixes == {"600"}
