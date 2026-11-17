from dataclasses import replace

from datetime import timedelta

import pytest

from tests.helpers.download.company import company_evidence as fixtures

from tests.helpers.download.company_resources import (
    CompanyDocumentFilesFixture, SQLiteCompanyFactsIndexFixture,
)



from trader.download.domain.company_events import CompanyPositiveStatus, CompanyReviewReason

from trader.download.infra.company_facts_index import CompanyInputReceipt, ReadCompanyPublicationIdentity

from trader.download.infra.company_field_publication import CompanyFieldProofRulesHash
from trader.download.infra.company_field_recovery import ReadCompanyFieldRecovery

from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction

from tests.helpers.download.company.company_field_recovery import (
    NOW,
    _DETAIL,
    _PLAN,
    _facts,
    _legacy_reduction,
    _legacy_repurchase,
    _put,
)

from trader.download.infra.company_link_codec import DecodeCompanyLink, EncodeCompanyLink
























@pytest.mark.parametrize(
    ("kind", "code", "expected_actual_gap"),
    (("announcement", "600002", True), ("news", "600001", True), ("announcement", "600001", False)),
    ids=("other-stock", "news", "same-stock-multiple-sources"),
)
def test_progress_field_recovery_scopes_titles_to_same_stock_announcements(tmp_path, kind, code, expected_actual_gap):
    index, item = _legacy_repurchase(tmp_path, "本公司拟回购股份，回购股份用于注销。" + _DETAIL)
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("UPDATE disclosures SET title='关于回购股份进展的公告'")
        collected.run(
            "INSERT INTO disclosures(source,source_key,kind,title,published_at,url,record_hash) "
            "SELECT 'exchange_disclosure',source_key,?,'普通公告',published_at,url,'collision' "
            "FROM disclosures WHERE source=? AND source_key=?",
            (kind, item.source, item.identity),
        )
        collected.run(
            "INSERT INTO disclosure_codes(code,disclosure_id) "
            "SELECT ?,id FROM disclosures WHERE source='exchange_disclosure'",
            (code,),
        )
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal:
        plans = ReadCompanyFieldRecovery(formal, collected, item.codes, NOW, tmp_path, field_rule_hash=CompanyFieldProofRulesHash())
    actual = tuple(plan for plan in plans if plan.gap.field == "actual_pct")
    assert bool(actual) is expected_actual_gap
    if actual:
        assert len(actual) == 1 and actual[0].gap.code == item.codes[0]
        assert actual[0].gap.evidence_id == item.identity and actual[0].gap.reason == "measurement_missing"


def test_multistock_recovery_does_not_borrow_another_codes_same_id_material(tmp_path):
    index, item = _legacy_reduction(tmp_path)
    facts = _facts(index)
    body_hash = CompanyDocumentFilesFixture(tmp_path / "text").write(fixtures._extraction(_PLAN).text)
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("INSERT INTO company_population VALUES('600002')")
        collected.run("UPDATE disclosures SET body_retained=1,body_hash=?", (body_hash,))
    with SqliteSession(index.facts_path) as formal:
        parent = ReadCompanyPublicationIdentity(formal)
    SQLiteCompanyFactsIndexFixture(index.facts_path).publish(
        (replace(facts, code="600002"),), {item.identity: item.source}, parent, NOW,
    )
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal:
        plans = ReadCompanyFieldRecovery(formal, collected, ("600001", "600002"), NOW, tmp_path, field_rule_hash=CompanyFieldProofRulesHash())
    assert {(plan.gap.code, plan.action, plan.reason) for plan in plans} == {
        ("600001", "extract", "local_text_available"),
        ("600002", "verify", "source_metadata_missing"),
    }


def test_multistock_recovery_keeps_same_id_material_and_event_times_with_their_code(tmp_path):
    index, item = _legacy_reduction(tmp_path)
    facts = _facts(index)
    other_time = NOW - timedelta(days=1)
    other_event = replace(
        facts.risk_events[0], first_disclosed_at=other_time, last_disclosed_at=other_time, effective_at=other_time,
    )
    body_hash = CompanyDocumentFilesFixture(tmp_path / "text").write(fixtures._extraction(_PLAN).text)
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("INSERT INTO company_population VALUES('600002')")
        collected.run("UPDATE disclosures SET published_at=?", ((NOW - timedelta(days=2)).isoformat(),))
        collected.run(
            "INSERT INTO disclosures(source,source_key,kind,title,published_at,url,record_hash,body_retained,body_hash) "
            "SELECT 'exchange_disclosure',source_key,kind,title,?,url,'other-record',1,? FROM disclosures",
            (other_time.isoformat(), body_hash),
        )
        collected.run(
            "INSERT INTO disclosure_codes(code,disclosure_id) "
            "SELECT '600002',id FROM disclosures WHERE source='exchange_disclosure'",
        )
    with SqliteSession(index.facts_path) as formal:
        parent = ReadCompanyPublicationIdentity(formal)
    SQLiteCompanyFactsIndexFixture(index.facts_path).publish(
        (replace(facts, code="600002", risk_events=(other_event,)),),
        {item.identity: "exchange_disclosure"}, parent, NOW,
    )
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal:
        plans = ReadCompanyFieldRecovery(formal, collected, ("600001", "600002"), NOW, tmp_path, field_rule_hash=CompanyFieldProofRulesHash())
    assert {(plan.gap.code, plan.action, plan.reason) for plan in plans} == {
        ("600001", "verify", "earlier_evidence_requires_revision_check"),
        ("600002", "extract", "local_text_available"),
    }


def test_attempt_on_material_removed_from_code_does_not_suppress_current_same_id_material(tmp_path):
    index, item = _legacy_reduction(tmp_path)
    event = _facts(index).risk_events[0]
    body_hash = CompanyDocumentFilesFixture(tmp_path / "text").write(fixtures._extraction(_PLAN).text)
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("INSERT INTO company_population VALUES('600002')")
        collected.run("UPDATE disclosures SET body_retained=1,body_hash=?", (body_hash,))
        collected.run(
            "INSERT INTO disclosures(source,source_key,kind,title,published_at,url,record_hash) "
            "SELECT 'exchange_disclosure',source_key,kind,title,published_at,url,'old-record' FROM disclosures",
        )
        collected.run(
            "INSERT INTO disclosure_codes(code,disclosure_id) "
            "SELECT '600002',id FROM disclosures WHERE source='exchange_disclosure'",
        )
        collected.run(
            "INSERT INTO company_field_repairs SELECT id,'600001',record_hash,?,'planned_pct',"
            "?,'attempted' FROM disclosures WHERE source='exchange_disclosure'",
            (event.event_id, 'reduction_total_capital_ratio:' + CompanyFieldProofRulesHash().hex()),
        )
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal:
        plans = ReadCompanyFieldRecovery(formal, collected, item.codes, NOW, tmp_path, field_rule_hash=CompanyFieldProofRulesHash())
        assert [(plan.action, plan.reason) for plan in plans] == [("extract", "local_text_available")]
        with SqliteTransaction(collected):
            collected.run(
                "INSERT INTO company_field_repairs SELECT id,'600001',record_hash,?,'planned_pct',"
                "?,'attempted' FROM disclosures WHERE source=?",
                (event.event_id, 'reduction_total_capital_ratio:' + CompanyFieldProofRulesHash().hex(), item.source),
            )
        plans = ReadCompanyFieldRecovery(formal, collected, item.codes, NOW, tmp_path, field_rule_hash=CompanyFieldProofRulesHash())
        assert [(plan.action, plan.reason) for plan in plans] == [("verify", "same_rule_already_attempted")]
































def _same_body_planned_actual(tmp_path, title, statement):
    index = fixtures._index(tmp_path)
    index.publish(("600001",), NOW)
    item = fixtures._disclosure("actual-status", title)
    _put(index, item, statement + _DETAIL * 2)
    with SqliteSession(index.path) as collected:
        payload, body_hash = collected.run(
            "SELECT l.payload,d.body_hash FROM document_link_candidates l JOIN disclosures d "
            "ON d.source=l.source AND d.source_key=l.source_key AND d.record_hash=l.record_hash "
            "WHERE l.source_key=?", (item.identity,),
        ).require_one()
    candidate = DecodeCompanyLink(payload)
    legacy_event = replace(candidate.positive, status=CompanyPositiveStatus.PLANNED, positive_level=0, level_milestones=())
    legacy = replace(_facts(index), positive_events=(legacy_event,), pending_document_count=0, review_reasons=(
        CompanyReviewReason("positive", "implementation_or_measurement_unverified", item.identity, NOW, "旧实际比例待核验"),
    ))
    writer = SQLiteCompanyFactsIndexFixture(index.facts_path)
    with SqliteSession(index.facts_path) as formal:
        parent = ReadCompanyPublicationIdentity(formal)
    writer.publish(
        (legacy,), {item.identity: item.source}, parent, NOW,
        receipts=(CompanyInputReceipt(
            item.source, item.identity, item.codes[0], item.digest, writer.extraction_hash, body_hash, NOW,
        ),),
    )
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("UPDATE document_link_candidates SET payload=? WHERE source_key=?",
                      (EncodeCompanyLink(replace(candidate, positive=legacy_event)), item.identity))
        collected.run("INSERT OR REPLACE INTO document_publications SELECT id,?,?,?,? FROM disclosures WHERE source_key=?",
                      (item.codes[0], item.digest, writer.extraction_hash, NOW.isoformat(), item.identity))
        collected.run("DELETE FROM company_dirty_codes WHERE code=?", (item.codes[0],))
    return index, item, writer
