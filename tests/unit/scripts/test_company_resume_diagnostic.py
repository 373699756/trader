"""The public company diagnostic isolates writes and retains bounded recovery evidence."""

import json
import sqlite3
import sys

import pytest

from scripts.diagnose_runtime import DiagnosticResult, _parser, _validate, build_commands, build_report
from scripts.runtime_diagnostics.company_resume import Main, _copy, _saved_pages
from tests.unit.infra.test_company_sources import NOW
from trader.download.domain.company_evidence import DisclosurePage, DisclosureQuery
from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex


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


def test_backup_preserves_source_and_is_independently_writable(tmp_path):
    source, replica = tmp_path / "source.sqlite3", tmp_path / "replica.sqlite3"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE facts(value INTEGER)")
    connection.execute("INSERT INTO facts VALUES(7)")
    connection.commit()
    connection.close()
    before = source.read_bytes()
    # A stale intermediate name must never let the backup write through a source hard link.
    stale = replica.with_suffix(".copying.sqlite3")
    stale.hardlink_to(source)
    _copy(source, replica)
    connection = sqlite3.connect(replica)
    connection.execute("UPDATE facts SET value=9")
    connection.commit()
    connection.close()
    assert source.read_bytes() == before and stale.samefile(source)


def test_discovery_response_is_not_counted_as_a_saved_page_replayed_by_parent_verification(tmp_path):
    index = SQLiteCompanyEvidenceIndex(tmp_path / "evidence.sqlite3")
    index.initialize()
    root = index.partitions(("sse",), NOW)[0]
    child = index.split_query(root, (DisclosureQuery("shmb"), DisclosureQuery("shkcp")))[0]
    index.commit_page(child, DisclosurePage((), 0, False), NOW)
    parent_key = (root.market, str(root.start), str(root.end), "", 1)
    child_key = (child.market, str(child.start), str(child.end), child.query.key, 1)
    assert _saved_pages(index.path, [parent_key, child_key]) == {child_key}


def test_replay_rejects_hard_link_to_active_database_before_any_write(tmp_path, monkeypatch):
    source = tmp_path / "active.sqlite3"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE facts(value INTEGER)")
    connection.close()
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
