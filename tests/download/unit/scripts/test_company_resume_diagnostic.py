"""The public company diagnostic isolates writes and retains bounded recovery evidence."""

from tests.helpers.shared.tool_cli import BuildToolCommands, ParseBusinessOptions, ParseToolArguments

from scripts.core.catalog import SelectionArguments

from tests.helpers.shared.configuration import SqlitePolicyFixture

import json
import sys
import time
from contextlib import nullcontext
from dataclasses import replace
from datetime import timedelta

import pytest

from scripts.core.contracts import DiagnosticResult
from scripts.core.projections import BuildReport
from scripts.download.company import company_pipeline_audit
from scripts.download.company import company_ocr_audit
from scripts.download.company.company_resume import Main
from scripts.download.company.company_resume import _copy
from tests.helpers.download.company import company_evidence as fixtures
from trader.download.domain.company_evidence import CompanyDocumentFailure, DisclosurePage
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction


@pytest.fixture
def offline_pipeline_sources(monkeypatch):
    """The explicit document probe has no structured-source or default-sync client."""
    monkeypatch.setattr(company_pipeline_audit, "OpenExternalHttp", lambda: nullcontext(None))


def test_ocr_comparison_reuses_seven_identical_pdfs_without_writing_collection(tmp_path, monkeypatch):
    import hashlib
    from contextlib import nullcontext
    from tests.helpers.download.company.company_pipeline import _seed

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


@pytest.mark.parametrize("failure", ["disclosure_document_ocr_timeout", "disclosure_document_ocr_timeout_exhausted"])
@pytest.mark.parametrize("corrupt", [False, True])
def test_ocr_timeout_probe_copies_local_failed_pdf_without_downloading(tmp_path, monkeypatch, failure, corrupt):
    import hashlib

    from tests.helpers.download.company.company_pipeline import _seed
    from trader.download.infra.company_raw_files import CompanyRawFiles

    index = fixtures._index(tmp_path)
    item = _seed(index, 1)[0]
    payload = b"%PDF-failed-original"
    digest = hashlib.sha256(payload).digest()
    index.document_failure(item, "old-policy", failure, fixtures.NOW, fixtures.NOW, digest)
    raw = CompanyRawFiles(
        index.path.parent, capacity=1024 * 1024, analysis_reserve=1024, free_reserve=1, maximum_document_bytes=1024,
    )
    directory = raw.directory(item)
    directory.mkdir(parents=True, exist_ok=True)
    original = directory / (digest.hex() + ".pdf")
    original.write_bytes(payload + (b"corrupt" if corrupt else b""))

    class Source:
        def __init__(self, *args, **kwargs):
            pass

        def download_document(self, item):
            pytest.fail("a retained local PDF must not be downloaded again")

    class Engine:
        def __init__(self, *args, **kwargs):
            pass

        def extract(self, path, expected):
            assert path.read_bytes() == payload and expected == digest
            return "合格正文" * 30, 90.0

    monkeypatch.setattr(company_ocr_audit, "OpenExternalHttp", lambda: nullcontext(None))
    monkeypatch.setattr(company_ocr_audit, "CninfoMarketDisclosureSource", Source)
    monkeypatch.setattr(company_ocr_audit, "CompanyOcrEngine", Engine)
    before = index.path.read_bytes(), original.read_bytes()
    report = company_ocr_audit.AuditCompanyOcr(index.path, tmp_path / "probe", budget_seconds=30, sqlite_policy=SqlitePolicyFixture())
    result = report["summary"]["ocr_audit"]
    assert result["sample_size"] == 1 and not result["active_writes"]
    assert result["passed"] is not corrupt
    assert result["recovered_documents"] == int(not corrupt)
    assert (index.path.read_bytes(), original.read_bytes()) == before
    if not corrupt:
        assert result["samples"][0]["input_source"] == "local"




@pytest.mark.parametrize("fault", (None, "duplicate", "revised_duplicate", "truncated", "changing_total", "oversized"))
def test_pacing_directory_probe_requires_complete_unique_stable_record_sets(fault):
    from trader.download.domain.company_evidence import DisclosurePartition

    day = fixtures.NOW.date()
    items = tuple(fixtures._disclosure(f"pacing-{number}") for number in range(31))

    class Source:
        def fetch(self, partition):
            if fault == "oversized":
                return DisclosurePage(items[:30], 601, True)
            if partition.page == 1:
                return DisclosurePage(items[:30], 31, True)
            if fault == "duplicate":
                return DisclosurePage(items[:1], 31, False)
            if fault == "revised_duplicate":
                return DisclosurePage((replace(items[0], digest="revised"),), 31, False)
            if fault == "truncated":
                return DisclosurePage((), 31, False)
            return DisclosurePage(items[30:], 32 if fault == "changing_total" else 31, False)

    result = company_pipeline_audit._probe_directory(Source(), DisclosurePartition("sse", day, day))
    assert result["complete"] is (fault is None)
    if fault is None:
        assert result["rows"] == 31 and result["pages"] == 2 and len(result["digest"]) == 64
    else:
        assert result["failure"] == "validation_error" and "digest" not in result


@pytest.mark.parametrize("mode", ("pipeline", "ocr"))
def test_pipeline_audit_requires_external_directory_and_routes_to_its_owner(tmp_path, mode):
    with pytest.raises(ValueError, match="company-replay-directory"):
        ParseBusinessOptions(ParseToolArguments([*SelectionArguments('company-resume'), f"--company-{mode}-audit"]))
    options = ParseBusinessOptions(
        ParseToolArguments(
            [*SelectionArguments('company-resume'), f"--company-{mode}-audit", "--company-replay-directory", str(tmp_path)]
        )
    )
    assert f"--{mode}-only" in BuildToolCommands(options)[0].argv


def test_pipeline_probe_compares_facts_without_observation_time_noise(tmp_path, monkeypatch, offline_pipeline_sources):
    now = company_pipeline_audit.datetime.now(fixtures.NOW.tzinfo)
    items = tuple(fixtures._disclosure(str(i), "本公司收到立案告知书", when=now) for i in range(2))

    calls = []
    class Source(company_pipeline_audit.CninfoMarketDisclosureSource):
        def __init__(self, *args, **kwargs):
            self.downloaded = set()

        def download_document(self, item):
            self.downloaded.add(item.identity)
            calls.append(item.identity)

        def extract_document(self, item, *, route="standard"):
            assert item.identity in self.downloaded
            return fixtures._extraction("本公司收到中国证监会立案告知书。")

    monkeypatch.setattr(company_pipeline_audit, "CninfoMarketDisclosureSource", Source)
    result = company_pipeline_audit.AuditCompanyPipeline(tmp_path / "input" / "unused.sqlite3", tmp_path / "probe",
        budget_seconds=120, sqlite_policy=SqlitePolicyFixture(), sample=items, observed_at=now)
    assert result["status"] == "passed", result
    assert result["summary"]["pipeline_audit"]["equivalent_facts"]
    assert result["summary"]["pipeline_audit"]["equivalent_documents"]
    assert sorted(calls) == ["0", "0", "1", "1"]
    assert not result["summary"]["pipeline_audit"]["source_admission_verified"]
    assert not result["summary"]["pipeline_audit"]["full_sync_verified"]
    for run in result["summary"]["pipeline_audit"]["runs"]:
        assert run["sample_documents"] == run["extracted_documents"] == 2
        assert run["restart_unchanged"] and not run["failures"]


def test_pipeline_rejects_changed_pdf_even_when_extracted_text_and_facts_match(tmp_path, monkeypatch, offline_pipeline_sources):
    now = company_pipeline_audit.datetime.now(fixtures.NOW.tzinfo)
    items = (fixtures._disclosure("changed", "收到立案告知书", when=now),)
    clients = []

    class Source(company_pipeline_audit.CninfoMarketDisclosureSource):
        def __init__(self, *args, **kwargs):
            self.digest = b"a" * 32 if not clients else b"b" * 32
            self.downloaded = set()
            clients.append(self)

        def download_document(self, item):
            self.downloaded.add(item.identity)

        def extract_document(self, item, *, route="standard"):
            assert item.identity in self.downloaded
            return replace(fixtures._extraction("本公司收到中国证监会立案告知书。"), payload_hash=self.digest)

    monkeypatch.setattr(company_pipeline_audit, "CninfoMarketDisclosureSource", Source)
    report = company_pipeline_audit.AuditCompanyPipeline(tmp_path / "input" / "unused.sqlite3", tmp_path / "probe",
        budget_seconds=120, sqlite_policy=SqlitePolicyFixture(), sample=items, observed_at=now)
    summary = report["summary"]["pipeline_audit"]
    assert summary["equivalent_facts"] and not summary["equivalent_documents"]
    assert report["status"] == "degraded"
    assert all(run["extracted_documents"] == 1 for run in summary["runs"])


def test_company_profile_requires_explicit_external_directory(tmp_path):
    with pytest.raises(ValueError, match="company-replay-directory"):
        ParseBusinessOptions(ParseToolArguments([*SelectionArguments('company-resume')]))
    with pytest.raises(ValueError, match="outside the repository"):
        ParseBusinessOptions(ParseToolArguments([*SelectionArguments('company-resume'), "--company-replay-directory", "data/replay"]))
    options = ParseBusinessOptions(
        ParseToolArguments([*SelectionArguments('company-resume'), "--company-replay-directory", str(tmp_path)])
    )
    commands = BuildToolCommands(options)
    assert len(commands) == 1 and commands[0].name == "company_resume"
    assert str(tmp_path) in commands[0].argv
    audit = ParseBusinessOptions(
        ParseToolArguments(
            [
                *SelectionArguments('company-resume'),
                "--company-replay-directory",
                str(tmp_path),
                "--company-query-audit",
            ]
        )
    )
    assert "--audit-only" in BuildToolCommands(audit)[0].argv


def test_pipeline_probe_does_not_pass_when_both_runs_have_only_deterministic_failures(tmp_path, monkeypatch, offline_pipeline_sources):
    now = company_pipeline_audit.datetime.now(fixtures.NOW.tzinfo)
    items = (fixtures._disclosure("failure", "收到立案告知书", when=now),)
    capabilities = []

    class Source(company_pipeline_audit.CninfoMarketDisclosureSource):
        def __init__(self, *args, **kwargs):
            capabilities.append((kwargs["ocr_engine"], kwargs["text_capability"], kwargs["document_policies"]))
            self.downloaded = set()

        def download_document(self, item):
            self.downloaded.add(item.identity)

        def extract_document(self, item, *, route="standard"):
            assert item.identity in self.downloaded
            raise CompanyDocumentFailure("disclosure_document_ocr_low_quality", payload_hash=b"x" * 32)

    monkeypatch.setattr(company_pipeline_audit, "CninfoMarketDisclosureSource", Source)
    report = company_pipeline_audit.AuditCompanyPipeline(tmp_path / "input" / "unused.sqlite3", tmp_path / "probe",
        budget_seconds=120, sqlite_policy=SqlitePolicyFixture(), sample=items, observed_at=now)
    assert capabilities and all(all(item is not None for item in row) for row in capabilities)
    assert report["status"] == "degraded"
    assert report["summary"]["pipeline_audit"]["equivalent_facts"]
    for run in report["summary"]["pipeline_audit"]["runs"]:
        assert run["extracted_documents"] == 0
        assert run["failures"] == ["disclosure_document_ocr_low_quality"]




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
    from scripts.download.company.company_query_audit import _Capture
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
