"""Publication coverage and receipt diagnostics must not turn missing evidence into success."""

from tests.helpers.shared.configuration import SqlitePolicyFixture

import time

import pytest

from scripts.download.company.company_publication_audit import AuditCompanyPublication
from scripts.download.company.company_query_audit import AuditCompanyDatabase
from tests.helpers.download.company import company_evidence as fixtures
from trader.download.domain.company_evidence import DisclosurePage
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction




def _audit(index, now=fixtures.NOW):
    return AuditCompanyPublication(index.path, index.facts_path, now=now, deadline=time.monotonic() + 10, sqlite_policy=SqlitePolicyFixture())
























def test_known_population_without_formal_publication_cannot_pass(tmp_path):
    index = fixtures._index(tmp_path)
    result = AuditCompanyDatabase(index.path, facts_path=index.facts_path, now=fixtures.NOW, budget_seconds=10, sqlite_policy=SqlitePolicyFixture())
    assert result["status"] == "degraded"
    assert "company_publication_missing" in result["findings"]
    assert "company_population_unpublished" in result["findings"]






@pytest.mark.parametrize("diagnostic", [
    "stage=download;record=first;version=a", "object_id=first", "object_id=first > revision=a > revision=b",
    "object_id=first > revision=a > object_id=second", "object_id=first > revision=a/b",
])
def test_failure_identity_rejects_retired_or_ambiguous_diagnostics(diagnostic):
    from scripts.download.company.company_failure_audit import ParseCompanyFailureIdentity

    assert ParseCompanyFailureIdentity(diagnostic) is None














def test_local_material_inspection_verifies_text_and_compares_formal_contract_without_writes(tmp_path):
    from scripts.download.company.company_field_audit import InspectCompanyFieldMaterial
    from tests.helpers.download.company.company_annual_income import _contract

    index = fixtures._index(tmp_path, text_root=tmp_path / "raw" / "text")
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
    assert item["reparsed_contract"]["binding_confirmed"]
    assert item["reparsed_contract"]["contract_amount"] == 200_000_000
    assert item["reparsed_contract"]["annual_revenue"] is None and item["reparsed_contract"]["published"] is False
    pending = {field["field"]: field for field in item["unverified_fields"]}
    assert set(pending) == {"annual_revenue", "revenue_disclosed_at", "contract_revenue_pct", "amount_basis"}
    assert all(field["code"] == "600001" and field["state"] == "verification_required" for field in pending.values())
    assert all(field["evidence"] == [] for field in pending.values())
    assert all(field["code"] == "600001" for field in item["field_conclusions"])
    assert not item["unverified_fields_truncated"]
    assert (index.path.read_bytes(), index.facts_path.read_bytes()) == before


@pytest.mark.parametrize("keys", ((), ("x",) * 7, ("duplicate", "duplicate")))
def test_material_inspection_rejects_unbounded_or_duplicate_keys_before_io(tmp_path, keys):
    from scripts.download.company.company_field_audit import InspectCompanyFieldMaterial

    with pytest.raises(ValueError, match="company_material_keys_invalid"):
        InspectCompanyFieldMaterial(tmp_path / "missing.sqlite3", tmp_path / "missing-facts.sqlite3", keys, sqlite_policy=SqlitePolicyFixture(),
                                    observed_at=fixtures.NOW, event_policy=fixtures.CompanyEventPolicy())
    assert not tuple(tmp_path.iterdir())


def test_material_pending_fields_remain_visible_without_text_and_exclude_other_events(tmp_path, monkeypatch):
    from scripts.download.company import company_field_audit
    from tests.helpers.download.company.company_annual_income import _contract
    from trader.download.domain.company_evidence import DisclosurePartition

    index = fixtures._index(tmp_path, text_root=tmp_path / "raw" / "text")
    _contract(index)
    other = fixtures._disclosure("another-contract", "关于签订重大合同的公告")
    index.commit_page(DisclosurePartition("sse", other.published_at.date(), other.published_at.date()),
                      DisclosurePage((other,), 1, False), fixtures.NOW)
    index.commit_document(other, fixtures._extraction(
        "本公司已签订正式合同。合同金额人民币1亿元。相关年度收入和税收口径尚待核实。"
    ), fixtures.NOW)
    index.publish(other.codes, fixtures.NOW)
    before = (index.path.read_bytes(), index.facts_path.read_bytes())
    def missing_text(_self, _digest):
        raise FileNotFoundError("isolated unavailable source")
    monkeypatch.setattr(company_field_audit.CompanyDocumentFiles, "read", missing_text)

    report = company_field_audit.InspectCompanyFieldMaterial(
        index.path, index.facts_path, ("contract-income",), sqlite_policy=SqlitePolicyFixture(),
        observed_at=fixtures.NOW, event_policy=fixtures.CompanyEventPolicy(),
    )

    item, = report["items"]
    assert item["material_available"] is False and item["material_failure"] == "FileNotFoundError"
    assert item["current_receipt"] and "reparsed_contract" not in item
    assert len(item["unverified_fields"]) == 4
    assert {field["event_id"] for field in item["unverified_fields"]} == {"contract-income:operating_contract"}
    assert all(field["state"] == "verification_required" and field["evidence"] == [] for field in item["unverified_fields"])
    assert not report["field_correctness_verified"] and (index.path.read_bytes(), index.facts_path.read_bytes()) == before


def test_material_inspection_does_not_count_a_previous_extraction_receipt_as_current(tmp_path):
    from scripts.download.company.company_field_audit import InspectCompanyFieldMaterial
    from tests.helpers.download.company.company_annual_income import _contract

    index = fixtures._index(tmp_path, text_root=tmp_path / "raw" / "text")
    _contract(index)
    with SqliteSession(index.facts_path) as formal, SqliteTransaction(formal, immediate=True):
        formal.run("UPDATE company_input_receipt SET extraction_hash=? WHERE outcome='valid'", (b"x" * 32,))
    before = (index.path.read_bytes(), index.facts_path.read_bytes())
    report = InspectCompanyFieldMaterial(index.path, index.facts_path, ("contract-income",), sqlite_policy=SqlitePolicyFixture(),
                                        observed_at=fixtures.NOW, event_policy=fixtures.CompanyEventPolicy())
    assert report["stable_read"] and report["items"][0]["material_available"]
    assert report["items"][0]["current_receipt"] is False
    assert (index.path.read_bytes(), index.facts_path.read_bytes()) == before
