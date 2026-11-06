"""Field-rule repairs resume through the existing publication and collection owners."""

from dataclasses import replace
from datetime import date, timedelta
from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.shared.configuration import CompanyDocumentFilesFixture, SQLiteCompanyEvidenceReaderFixture, SQLiteCompanyFactsIndexFixture
from trader.download.domain.company_event_extraction import ExtractCompanyEvents
from trader.download.domain.company_evidence import CompanyDocument, DisclosurePage, DisclosurePartition
from trader.download.domain.company_events import CompanyEventStatus, CompanyPositiveStatus, CompanyReviewReason, CompanyRiskEvent, CompanyRiskType, CompanySeverityBasis, CompanySubject, ReductionPlan, RepurchasePurpose
from trader.download.infra.company_facts_index import CompanyInputReceipt, ReadCompanyPublicationIdentity
from trader.download.infra.company_field_recovery import CompanyFieldRepairReceipts
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction

from trader.download.domain.company_event_extraction import CompanyLinkCandidate
from trader.download.infra.company_facts_protocol import COMPANY_EXTRACTION_IDENTITY
from trader.download.infra.company_link_codec import EncodeCompanyLink

NOW = fixtures.NOW

_DETAIL = "本公告仅说明该事项的既有安排。相关信息请以本次正式披露为准。本公司将按照相关规定及时披露后续进展情况。"

_PLAN = "本公司控股股东拟减持股份，占公司总股本的比例为2%，减持期间为2026年10月19日至2026年11月19日。" + _DETAIL

_PLAN_WITHOUT_RATIO = "本公司控股股东拟减持股份，减持期间为2026年10月19日至2026年11月19日。" + _DETAIL


def _put(index, item, body):
    partition = DisclosurePartition("sse", item.published_at.date(), item.published_at.date())
    index.commit_page(partition, DisclosurePage((item,), 1, False), NOW)
    assert index.commit_document(item, fixtures._extraction(body), NOW)


def _facts(index):
    return SQLiteCompanyEvidenceReaderFixture(index.facts_path).read(("600001",))[0]


def _state(index):
    with SqliteSession(index.path) as connection:
        return tuple(connection.run(
            "SELECT field,state FROM company_field_repairs ORDER BY field"
        ))


def _legacy_reduction(tmp_path, body=_PLAN):
    """Construct an old valid receipt and missing-field fact without using the new parser."""
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("original-plan", "控股股东减持计划公告")
    partition = DisclosurePartition("sse", item.published_at.date(), item.published_at.date())
    index.commit_page(partition, DisclosurePage((item,), 1, False), NOW)
    index.publish(item.codes, NOW)
    facts = _facts(index)
    event = CompanyRiskEvent(
        "legacy-reduction",
        CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION,
        4,
        CompanySubject.CONTROLLING_SHAREHOLDER,
        CompanySeverityBasis.HARD,
        None,
        NOW,
        NOW,
        NOW,
        CompanyEventStatus.ONGOING,
        None,
        None,
        "大股东减持",
        (item.identity,),
        ReductionPlan(date(2026, 10, 19), date(2026, 11, 19)),
    )
    legacy = replace(
        facts, risk_events=(event,), pending_document_count=0,
        review_reasons=tuple(review for review in facts.review_reasons if review.evidence_id != item.identity),
    )
    formal_index = SQLiteCompanyFactsIndexFixture(index.facts_path)
    extraction = fixtures._extraction(body)
    with SqliteSession(index.facts_path) as formal:
        parent = ReadCompanyPublicationIdentity(formal)
    formal_index.publish(
        (legacy,), {item.identity: item.source}, parent, NOW,
        receipts=(CompanyInputReceipt(
            item.source, item.identity, item.codes[0], item.digest, formal_index.extraction_hash,
            extraction.payload_hash, NOW,
        ),),
    )
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run(
            "UPDATE disclosures SET body_checked=1,body_retained=0,body_hash=? WHERE source=? AND source_key=?",
            (extraction.payload_hash, item.source, item.identity),
        )
        collected.run(
            "INSERT INTO document_publications SELECT id,?,?,?,? FROM disclosures WHERE source=? AND source_key=?",
            (item.codes[0], item.digest, formal_index.extraction_hash, NOW.isoformat(), item.source, item.identity),
        )
    assert _facts(index).risk_events[0].reduction_plan.planned_ratio_pct is None
    assert _state(index) == ()
    assert not tuple((tmp_path / "text").rglob("*.gz"))
    return index, item


def _legacy_repurchase(tmp_path, body):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("purpose-plan", "关于回购股份的公告")
    index.commit_page(
        DisclosurePartition("sse", NOW.date(), NOW.date()), DisclosurePage((item,), 1, False), NOW,
    )
    index.publish(item.codes, NOW)
    facts = _facts(index)
    extraction = fixtures._extraction(body)
    parsed = ExtractCompanyEvents((CompanyDocument(item, extraction.text, True),), NOW, fixtures.CompanyEventPolicy())
    event = replace(
        parsed.positives[0], measurements=replace(parsed.positives[0].measurements, purpose=RepurchasePurpose.UNKNOWN),
        positive_level=0, level_milestones=(), not_eligible_reason="implementation_or_measurement_unverified",
    )
    review = CompanyReviewReason("positive", "implementation_or_measurement_unverified", item.identity, NOW, "旧规则用途待核验")
    legacy = replace(
        facts, positive_events=(event,), pending_document_count=0,
        review_reasons=(*tuple(r for r in facts.review_reasons if r.evidence_id != item.identity), review),
    )
    body_hash = CompanyDocumentFilesFixture(tmp_path / "text").write(extraction.text)
    formal_index = SQLiteCompanyFactsIndexFixture(index.facts_path)
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("UPDATE disclosures SET body_checked=1,body_retained=1,body_hash=?", (body_hash,))
        collected.run(
            "INSERT INTO company_field_repairs SELECT id,?,record_hash,?,'repurchase_purpose',"
            "'required_field_evidence:1','pending' FROM disclosures",
            (item.codes[0], event.event_id),
        )
        old_attempts = CompanyFieldRepairReceipts(collected, item.codes, NOW)
    with SqliteSession(index.facts_path) as formal:
        parent = ReadCompanyPublicationIdentity(formal)
    formal_index.publish(
        (legacy,), {item.identity: item.source}, parent, NOW,
        receipts=(CompanyInputReceipt(item.source, item.identity, item.codes[0], item.digest,
                                     formal_index.extraction_hash, body_hash, NOW), *old_attempts),
    )
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("UPDATE company_field_repairs SET state='attempted'")
        collected.run(
            "INSERT INTO document_publications SELECT id,?,?,?,? FROM disclosures",
            (item.codes[0], item.digest, formal_index.extraction_hash, NOW.isoformat()),
        )
    return index, item


def _publish_latest_ratio(index, item, *, different_event=False):
    prior = _facts(index)
    event = prior.risk_events[0]
    latest = replace(
        event, event_id="independent-reduction" if different_event else event.event_id,
        last_disclosed_at=NOW + timedelta(minutes=1),
        reduction_plan=replace(event.reduction_plan, planned_ratio_pct=2),
    )
    updated = replace(prior, risk_events=(event, latest) if different_event else (latest,))
    with SqliteSession(index.facts_path) as formal:
        parent = ReadCompanyPublicationIdentity(formal)
    SQLiteCompanyFactsIndexFixture(index.facts_path).publish(
        (updated,), {item.identity: item.source}, parent, NOW + timedelta(minutes=1),
    )


def _legacy_repurchase_template(tmp_path):
    body = (
        "本公司首次回购股份100万股，占公司总股本的0.062%。"
        "回购用途□减少注册资本□用于员工持股计划或股权激励√用于转换公司可转债"
        "□为维护公司价值及股东权益。" + _DETAIL
    )
    index, item = _legacy_repurchase(tmp_path, body)
    facts = _facts(index)
    current = facts.positive_events[0]
    old = replace(
        current, status=CompanyPositiveStatus.PLANNED,
        measurements=replace(current.measurements, actual_ratio_pct=None, purpose=RepurchasePurpose.EMPLOYEE_PLAN),
    )
    formal_index = SQLiteCompanyFactsIndexFixture(index.facts_path)
    with SqliteSession(index.facts_path) as formal:
        parent = ReadCompanyPublicationIdentity(formal)
    formal_index.publish((replace(facts, positive_events=(old,)),), {item.identity: item.source}, parent, NOW)
    return index, item


def _legacy_holder_table(tmp_path, title="关于回购股份事项前十大股东持股情况的公告"):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("holder-table", title)
    body = (item.title + "。本公司董事会保证本次披露真实。公司董事会已审议通过回购股份议案，"
            "现公布决议前一交易日的前十大股东持股情况。" + _DETAIL)
    index.commit_page(DisclosurePartition("sse", item.published_at.date(), item.published_at.date()),
                      DisclosurePage((item,), 1, False), NOW)
    index.publish(item.codes, NOW)
    index.commit_page(DisclosurePartition("sse", item.published_at.date(), item.published_at.date()),
                      DisclosurePage((item,), 1, False), NOW)
    assert index.commit_document(item, fixtures._extraction(body), NOW)
    legacy = ExtractCompanyEvents((CompanyDocument(replace(item, title="关于回购股份的公告"), "本公司拟回购股份。", True),),
                                  NOW, fixtures.CompanyEventPolicy()).positives[0]
    with SqliteSession(index.facts_path) as formal:
        parent = ReadCompanyPublicationIdentity(formal)
    with SqliteSession(index.path) as collected:
        digest = collected.run("SELECT body_hash FROM disclosures WHERE source_key=?", (item.identity,)).require_one()[0]
    SQLiteCompanyFactsIndexFixture(index.facts_path).publish(
        (replace(_facts(index), positive_events=(legacy,), review_reasons=(
            CompanyReviewReason("positive", "implementation_or_measurement_unverified", item.identity, NOW, "旧空占位"),
        )),), {item.identity: item.source}, parent, NOW,
        receipts=(CompanyInputReceipt(item.source, item.identity, item.codes[0], item.digest,
                                      parent.extraction_hash, digest, NOW),),
    )
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        disclosure_id = collected.run("SELECT id FROM disclosures WHERE source_key=?", (item.identity,)).require_one()[0]
        collected.run("INSERT OR REPLACE INTO document_publications VALUES(?,?,?,?,?)",
                      (disclosure_id, item.codes[0], item.digest, parent.extraction_hash, NOW.isoformat()))
        collected.run("INSERT OR REPLACE INTO document_link_candidates VALUES(?,?,?,?,?)",
                      (item.source, item.identity, item.digest, parent.extraction_hash,
                       EncodeCompanyLink(CompanyLinkCandidate(item.identity, item.published_at, legacy.subject_type,
                                                             (), (), (), legacy, False))))
        collected.run("INSERT OR IGNORE INTO company_field_repairs VALUES(?,?,?,?,?,?,'attempted')",
                      (disclosure_id, item.codes[0], item.digest, legacy.event_id,
                       "repurchase_purpose", "explicit_repurchase_purpose:1"))
        collected.run("INSERT OR IGNORE INTO company_document_requirements VALUES(?)", (disclosure_id,))
        collected.run("UPDATE company_work_policy SET work_policy_id=?",
                      (COMPANY_EXTRACTION_IDENTITY + ":applicable-subjects:1:repurchase-events:1",))
    return index, item, legacy
