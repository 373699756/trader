"""Current-rule reproof ignores cached success and never changes published facts."""

import json

import pytest

from scripts.core.contracts import DiagnosticResult
from scripts.core.projections import BuildReport
from scripts.download.company import company_field_proof_audit as audit
from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company.company_field_recovery import _put
from tests.helpers.download.company_resources import CompanyDocumentFilesFixture
from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.shared.tool_cli import BuildToolCommands, ParseBusinessOptions, ParseToolArguments
from trader.download.infra import company_field_publication
from trader.infra.sqlite_connection import SqliteSession
from trader.infra.sqlite_publication import PublishSqliteFile

_TEXT = (
    "证券代码：600001\n本公司控股股东拟减持股份，占公司总股本的比例为2%。\n"
    "减持期间为2026年10月28日至2026年11月28日。\n"
    "本公告仅说明本次事项的实际执行情况。相关安排已经正式披露，公司将根据实际执行情况及时披露后续进度。"
)


def _index(tmp_path):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("proof-audit", "控股股东减持计划公告")
    _put(index, item, _TEXT)
    index.publish(item.codes, fixtures.NOW)
    CompanyDocumentFilesFixture(index.path.parent / "raw" / "text").write(_TEXT)
    with SqliteSession(index.path) as collected:
        collected.run("UPDATE disclosures SET body_retained=1 WHERE source_key=?", (item.identity,))
    return index


def _audit(index):
    return audit.AuditCompanyFieldProofs(
        index.path, index.facts_path, now=fixtures.NOW, budget_seconds=30,
        sqlite_policy=SqlitePolicyFixture(), event_policy=fixtures.CompanyEventPolicy(),
    )


def test_reproof_reads_current_originals_and_leaves_both_databases_unchanged(tmp_path):
    index = _index(tmp_path)
    before = index.path.read_bytes(), index.facts_path.read_bytes()
    report = _audit(index)
    result = report["summary"]["field_proof_audit"]
    assert report["status"] == "passed" and result["complete"] and result["stable_read"]
    assert result["target_fields"] == result["checked_fields"] == result["reproved_fields"] == 3
    assert result["material_reads"] > 0 and not result["unreproved_fields"]
    assert before == (index.path.read_bytes(), index.facts_path.read_bytes())
    assert not result["network_requests"] and not result["source_data_writes"]
    assert all(result[key] is False for key in ("production_adopted", "field_correctness_verified", "full_market_verified", "http_checked"))


def test_cached_verified_proofs_cannot_hide_current_rule_rejection(tmp_path, monkeypatch):
    index = _index(tmp_path)
    before = index.path.read_bytes(), index.facts_path.read_bytes()
    monkeypatch.setattr(company_field_publication, "ExtractCompanyFieldEvidence", lambda *args: ())
    report = _audit(index)
    result = report["summary"]["field_proof_audit"]
    assert report["status"] == "degraded" and not result["complete"]
    assert result["target_fields"] == result["unreproved_fields"] == 3 and result["reproved_fields"] == 0
    assert result["material_reads"] > 0
    assert "company_published_fields_not_reproved" in report["findings"]
    assert before == (index.path.read_bytes(), index.facts_path.read_bytes())


def test_independent_collection_commit_prevents_a_stable_reproof_pass(tmp_path, monkeypatch):
    index = _index(tmp_path)
    prepare = audit.PrepareCompanyFieldResults

    def concurrent_commit(*args, **kwargs):
        result = prepare(*args, **kwargs)
        with SqliteSession(index.path) as producer:
            producer.run("INSERT INTO company_dirty_codes VALUES('600009')")
        return result

    monkeypatch.setattr(audit, "PrepareCompanyFieldResults", concurrent_commit)
    report = _audit(index)
    result = report["summary"]["field_proof_audit"]
    assert result["reproved_fields"] == result["target_fields"] == 3
    assert report["status"] == "degraded" and not result["stable_read"] and not result["complete"]
    assert "company_audit_concurrent_change" in report["findings"]


def test_empty_verified_population_does_not_certify_field_reproof(tmp_path):
    index = fixtures._index(tmp_path)
    index.publish(("600001",), fixtures.NOW)
    report = _audit(index)
    result = report["summary"]["field_proof_audit"]
    assert result["target_fields"] == result["checked_fields"] == 0
    assert report["status"] == "degraded" and not result["complete"]
    assert "company_field_proof_targets_empty" in report["findings"]


@pytest.mark.parametrize("failure", ("missing", "receipt", "body", "released", "released_missing"))
def test_missing_or_unbound_originals_cannot_reprove_published_success(tmp_path, failure):
    index = _index(tmp_path)
    if failure in {"released", "released_missing"}:
        with SqliteSession(index.path) as collected:
            collected.run("UPDATE disclosures SET body_retained=0")
    if failure in {"missing", "released_missing"}:
        for path in (index.path.parent / "raw" / "text").glob("*/*.txt.gz"):
            path.unlink()
    elif failure == "receipt":
        with SqliteSession(index.facts_path) as formal:
            formal.run("DELETE FROM company_input_receipt WHERE outcome='valid'")
        PublishSqliteFile(index.facts_path, sqlite_policy=SqlitePolicyFixture())
    elif failure == "body":
        with SqliteSession(index.path) as collected:
            collected.run("UPDATE disclosures SET body_hash=?", (b"x" * 32,))
    before = index.path.read_bytes(), index.facts_path.read_bytes()
    report = _audit(index)
    result = report["summary"]["field_proof_audit"]
    assert report["status"] == "degraded" and result["reproved_fields"] == 0
    assert result["unreproved_fields"] == 3
    assert result["unreproved_basis"] == {dict(
        missing="original_body_missing", receipt="current_valid_receipt_missing", body="material_identity_changed",
        released="released_text_present", released_missing="original_body_released",
    )[failure]: 3}
    assert before == (index.path.read_bytes(), index.facts_path.read_bytes())


def test_own_budget_cancellation_cannot_be_swallowed_as_missing_material(tmp_path, monkeypatch):
    index = _index(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(audit.time, "monotonic", lambda: clock[0])

    def expired_read(self, digest, *, check=None):
        clock[0] = 31
        check()
        raise AssertionError("budget check must stop the read")

    monkeypatch.setattr(audit.CompanyDocumentFiles, "read", expired_read)
    report = _audit(index)
    result = report["summary"]["field_proof_audit"]
    assert report["status"] == "failed" and result["budget_exhausted"] and not result["complete"]
    assert not result["material_failures"]
    assert "company_field_proof_audit_budget_exhausted" in report["findings"]


def test_unrelated_interruption_is_preserved_even_when_the_budget_also_expires(tmp_path, monkeypatch):
    index = _index(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(audit.time, "monotonic", lambda: clock[0])
    error = InterruptedError("unrelated_interruption")

    def interrupted(*args, **kwargs):
        clock[0] = 31
        assert kwargs["cancel_requested"]()
        raise error

    monkeypatch.setattr(audit.SQLiteCompanyEvidenceReader, "read_fields", interrupted)
    with pytest.raises(InterruptedError) as captured:
        _audit(index)
    assert captured.value is error


def test_public_mode_rejects_replay_and_projects_only_bounded_evidence(tmp_path):
    arguments = ["--business", "download", "--module", "company", "--action", "coverage", "--company-field-proof-audit"]
    options = ParseBusinessOptions(ParseToolArguments(arguments))
    assert "--field-proof-only" in BuildToolCommands(options)[0].argv
    with pytest.raises(ValueError, match="does not accept a replay"):
        ParseBusinessOptions(ParseToolArguments([*arguments, "--company-replay-directory", str(tmp_path / "replay")]))
    index = _index(tmp_path)
    report = _audit(index)
    report["summary"]["field_proof_audit"]["private_payload"] = "never-publish-this"
    public = BuildReport("company-resume", (DiagnosticResult("company_resume", 0, 0.1, report, None),))
    assert public["status"] == "passed" and "never-publish-this" not in json.dumps(public)
    report["summary"]["field_proof_audit"]["field_correctness_verified"] = True
    rejected = BuildReport("company-resume", (DiagnosticResult("company_resume", 0, 0.1, report, None),))
    assert rejected["status"] == "failed"


@pytest.mark.parametrize("change", ("checked_fields", "reproved_fields", "current_extraction_hash"))
def test_public_reproof_rejects_boolean_counts_and_mismatched_policy(tmp_path, change):
    index = _index(tmp_path)
    report = _audit(index)
    result = report["summary"]["field_proof_audit"]
    result.update(target_fields=1, checked_fields=1, reproved_fields=1)
    result[change] = "0" * 64 if change == "current_extraction_hash" else True
    public = BuildReport("company-resume", (DiagnosticResult("company_resume", 0, 0.1, report, None),))
    assert public["status"] == "failed"
