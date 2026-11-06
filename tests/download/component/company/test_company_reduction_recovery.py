"""Retained, receipted reduction material is corrected through normal publication."""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company.company_field_recovery import _DETAIL, _facts, _put
from tests.helpers.shared.configuration import CompanyDocumentFilesFixture, SQLiteCompanyFactsIndexFixture
from trader.download.domain.company_event_extraction import CompanyLinkCandidate
from trader.download.domain.company_events import (
    CompanyEventStatus,
    CompanyReviewReason,
    CompanyRiskEvent,
    CompanyRiskType,
    CompanySeverityBasis,
    CompanySubject,
    ReductionPlan,
)
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.download.infra.company_field_recovery import (
    CompanyFieldRepairReceipts,
    CompleteCompanyFieldRepairs,
    QueueCompanyReductionRechecks,
)
from trader.download.infra.company_link_codec import EncodeCompanyLink
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction
from trader.recommendation.domain.scoring.company import IsCompanyRiskEffective

NOW = fixtures.NOW
_CLOSURE = "本公司控股股东根据公告original-plan披露减持计划进展，本次股份减持计划期限已届满，未减持股份。" + _DETAIL
_DATES = (
    "本公司控股股东计划减持股份，占公司总股本的比例为2%。"
    "减持期间：自减持计划披露之日起十五个交易日后的三个月内"
    "（即2026年11月4日至2027年2月3日）。" + _DETAIL
)


def _legacy(index, item, body, *, dates=False, shared=False, keep_original=False):
    original_events = _facts(index).risk_events if keep_original else ()
    _put(index, item, body)
    index.publish(item.codes, NOW)
    body_hash = CompanyDocumentFilesFixture(index.path.parent / "text").write(fixtures._extraction(body).text)
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run(
            "UPDATE disclosures SET body_hash=?,body_retained=1 WHERE source=? AND source_key=?",
            (body_hash, item.source, item.identity),
        )
    event = CompanyRiskEvent(
        f"{item.identity}:major_shareholder_reduction",
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
        item.title,
        (item.identity,),
        ReductionPlan(planned_ratio_pct=2) if dates else ReductionPlan(),
    )
    events = (event, replace(event, event_id="shared-event")) if shared else (event,)
    prior = _facts(index)
    if keep_original:
        events = (*original_events, *events)
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        if dates:
            for field in ("start_date", "end_date"):
                collected.run(
                    "INSERT OR REPLACE INTO company_field_repairs "
                    "SELECT id,?,record_hash,?,?,?,'pending' FROM disclosures WHERE source_key=?",
                    (item.codes[0], event.event_id, field, "explicit_reduction_" + field + ":1", item.identity),
                )
        old_attempts = CompanyFieldRepairReceipts(collected, item.codes, NOW)
    with SqliteSession(index.facts_path) as formal:
        parent = ReadCompanyPublicationIdentity(formal)
    SQLiteCompanyFactsIndexFixture(index.facts_path).publish(
        (replace(prior, risk_events=events, review_reasons=(), pending_document_count=0),),
        {key: item.source for event_row in events for key in event_row.evidence_ids},
        parent,
        NOW,
        receipts=old_attempts,
    )
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("UPDATE company_field_repairs SET state='attempted'")
        if not dates:
            stale = CompanyLinkCandidate(
                item.identity, NOW, event.subject_type, ("original-plan",), (),
                (CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION,), None, False,
            )
            collected.run(
                "INSERT OR REPLACE INTO document_link_candidates VALUES(?,?,?,?,?)",
                (item.source, item.identity, item.digest, parent.extraction_hash, EncodeCompanyLink(stale)),
            )
    return event


def _valid_receipts(index):
    with SqliteSession(index.facts_path) as formal:
        return tuple(tuple(row) for row in formal.run("SELECT * FROM company_input_receipt WHERE outcome='valid'"))


@pytest.mark.parametrize("body", (
    _CLOSURE,
    "本公司控股股东甲及乙根据公告original-plan披露减持计划进展，"
    "本次股份减持计划期限已届满，均未减持股份。" + _DETAIL,
    "本公司控股股东甲及乙披露减持计划。公司收到股东通知，"
    "本次股份减持计划期限已届满，均未减持股份。" + _DETAIL,
))
def test_legacy_closure_becomes_review_without_redownload_and_preserves_valid_receipt(tmp_path, body):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("old-closure", "控股股东减持计划期限届满暨未减持股份公告")
    _legacy(index, item, body)
    receipts = _valid_receipts(index)

    index.prepare_publication(item.codes, NOW + timedelta(minutes=1))

    facts = _facts(index)
    assert facts.risk_events == ()
    assert any(reason.reason_code == "resolution_unlinked" and reason.evidence_id == item.identity
               for reason in facts.review_reasons)
    assert _valid_receipts(index) == receipts
    assert index.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()
    with SqliteSession(index.path) as collected:
        assert collected.run("SELECT count(*) FROM company_field_repairs WHERE state='pending'").require_one()[0] == 0
    for minute in range(2, 4):
        restarted = fixtures._index(tmp_path)
        restarted.prepare_publication(item.codes, NOW + timedelta(minutes=minute))
        assert _facts(restarted).risk_events == ()
        assert _valid_receipts(restarted) == receipts
        assert restarted.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()


@pytest.mark.parametrize("guard", ("body_hash", "wrong_code", "multiple_codes", "shared_evidence", "missing_valid", "header_code"))
def test_reduction_recheck_keeps_unproven_or_shared_formal_risk(tmp_path, guard):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("old-closure", "控股股东减持计划期限届满暨未减持股份公告")
    body = "证券代码：600002\n" + _CLOSURE if guard == "header_code" else _CLOSURE
    _legacy(index, item, body, shared=guard == "shared_evidence")
    before_risks = _facts(index).risk_events
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        if guard == "wrong_code":
            collected.run("UPDATE disclosure_codes SET code='600002'")
        if guard == "multiple_codes":
            collected.run("INSERT INTO disclosure_codes(code,disclosure_id) SELECT '600002',id FROM disclosures")
        if guard == "body_hash":
            collected.run("UPDATE disclosures SET body_hash=?", (bytes(32),))
        before = tuple(tuple(row) for row in collected.run("SELECT * FROM document_publications"))
        collected.run("DELETE FROM company_dirty_codes")
        with SqliteSession(index.facts_path) as formal:
            parent = ReadCompanyPublicationIdentity(formal)
            QueueCompanyReductionRechecks(
                collected,
                formal,
                item.codes,
                NOW + timedelta(minutes=1),
                bytes(32) if guard == "missing_valid" else parent.extraction_hash,
                fixtures.CompanyEventPolicy(),
                CompanyDocumentFilesFixture(tmp_path / "text").read,
            )
        assert tuple(tuple(row) for row in collected.run("SELECT * FROM document_publications")) == before
        assert collected.run("SELECT count(*) FROM company_dirty_codes").require_one()[0] == 0
    assert _facts(index).risk_events == before_risks


def test_reduction_date_rule_two_recovers_previous_attempt_and_keeps_ratio(tmp_path):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("old-plan", "控股股东减持计划公告")
    _legacy(index, item, _DATES, dates=True)
    receipts = _valid_receipts(index)

    index.prepare_publication(item.codes, NOW + timedelta(minutes=1))

    plan = _facts(index).risk_events[0].reduction_plan
    assert (plan.planned_start_date, plan.planned_end_date, plan.planned_ratio_pct) == (
        date(2026, 11, 4), date(2027, 2, 3), 2,
    )
    with SqliteSession(index.path) as collected:
        rules = tuple(tuple(row) for row in collected.run(
            "SELECT field,rule_id,state FROM company_field_repairs WHERE rule_id IN (?,?) ORDER BY field",
            ("explicit_reduction_start_date:2", "explicit_reduction_end_date:2"),
        ))
    assert rules == (("end_date", "explicit_reduction_end_date:2", "complete"),
                     ("start_date", "explicit_reduction_start_date:2", "complete"))
    assert _valid_receipts(index) == receipts
    for minute in range(2, 4):
        restarted = fixtures._index(tmp_path)
        restarted.prepare_publication(item.codes, NOW + timedelta(minutes=minute))
        assert _facts(restarted).risk_events[0].reduction_plan == plan
        assert _valid_receipts(restarted) == receipts
        assert restarted.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()


@pytest.mark.parametrize("multiple_holders", (False, True))
def test_recovered_closure_keeps_original_plan_and_requires_an_unambiguous_holder(tmp_path, multiple_holders):
    index = fixtures._index(tmp_path)
    original = fixtures._disclosure("original-plan", "控股股东减持计划公告", when=NOW - timedelta(days=5))
    _put(index, original, "本公司控股股东拟减持股份，占公司总股本的比例为2%。"
         "减持期间为2026年10月19日至2026年11月19日。" + _DETAIL)
    index.publish(original.codes, NOW)
    original_event = _facts(index).risk_events[0]
    item = fixtures._disclosure("old-closure", "控股股东减持计划期限届满暨未减持股份公告")
    body = _CLOSURE.replace("控股股东根据", "控股股东甲及乙根据") if multiple_holders else _CLOSURE
    _legacy(index, item, body, keep_original=True)
    receipts = _valid_receipts(index)

    index.prepare_publication(item.codes, NOW + timedelta(minutes=1))

    event, = _facts(index).risk_events
    assert event.event_id == original_event.event_id
    assert event.reduction_plan.planned_start_date == original_event.reduction_plan.planned_start_date
    assert event.reduction_plan.planned_end_date == original_event.reduction_plan.planned_end_date
    assert event.reduction_plan.planned_ratio_pct == 2
    assert _valid_receipts(index) == receipts
    if multiple_holders:
        assert event.status is CompanyEventStatus.ONGOING
        assert event.resolved_at is None
        assert event.evidence_ids == original_event.evidence_ids
        assert any(review.reason_code == "resolution_unlinked" for review in _facts(index).review_reasons)
        with SqliteSession(index.path) as collected:
            assert collected.run(
                "SELECT count(*) FROM document_link_candidates WHERE source_key=?", (item.identity,),
            ).require_one()[0] == 0
    else:
        assert event.status is CompanyEventStatus.RESOLVED
        assert event.resolved_at == NOW
        assert event.reduction_plan.completion_disclosed_at == NOW
        assert IsCompanyRiskEffective(event, NOW + timedelta(days=90) - timedelta(microseconds=1))
        assert not IsCompanyRiskEffective(event, NOW + timedelta(days=90))


@pytest.mark.parametrize("receipt_guard", ("missing", "different_body"))
def test_retired_reduction_work_needs_its_exact_committed_field_attempt(tmp_path, receipt_guard):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("old-closure", "控股股东减持计划期限届满暨未减持股份公告")
    event = _legacy(index, item, _CLOSURE)
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run(
            "INSERT INTO company_field_repairs SELECT id,?,record_hash,?,'start_date',"
            "'explicit_reduction_start_date:2','pending' FROM disclosures WHERE source_key=?",
            (item.codes[0], event.event_id, item.identity),
        )
        attempts = CompanyFieldRepairReceipts(collected, item.codes, NOW)
    facts = replace(
        _facts(index), risk_events=(),
        review_reasons=(CompanyReviewReason("risk", "resolution_unlinked", item.identity, NOW, "原案待核验"),),
    )
    with SqliteSession(index.facts_path) as formal:
        parent = ReadCompanyPublicationIdentity(formal)
    receipts = tuple(replace(attempt, body_hash=bytes(32)) for attempt in attempts) if receipt_guard == "different_body" else ()
    SQLiteCompanyFactsIndexFixture(index.facts_path).publish(
        (facts,), {item.identity: item.source}, parent, NOW, receipts=receipts,
    )
    with SqliteSession(index.path) as collected, SqliteTransaction(collected), SqliteSession(index.facts_path) as formal:
        CompleteCompanyFieldRepairs(collected, formal, (facts,), parent.extraction_hash)
        assert collected.run("SELECT state FROM company_field_repairs").require_one()[0] == "pending"
