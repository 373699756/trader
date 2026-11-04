"""Field-rule repairs resume through the existing publication and collection owners."""

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.component import test_company_evidence as fixtures
from tests.helpers.configuration import SQLiteCompanyEvidenceReaderFixture, SQLiteCompanyFactsIndexFixture
from trader.download.domain.company_evidence import DisclosurePage, DisclosurePartition
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.download.infra.company_field_recovery import (
    CompanyFieldRepairReceipts,
    ReadCompanyFieldRecovery,
    RestoreCompanyFieldRepairs,
)
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction

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
    """Publish an old missing-field result without changing its source or valid receipt."""
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("original-plan", "控股股东减持计划公告")
    _put(index, item, body)
    index.publish(item.codes, NOW)
    facts = _facts(index)
    event = facts.risk_events[0]
    if body == _PLAN:
        assert event.reduction_plan.planned_ratio_pct == 2
    if event.reduction_plan.planned_ratio_pct is not None:
        legacy = replace(
            facts,
            risk_events=(replace(event, reduction_plan=replace(event.reduction_plan, planned_ratio_pct=None)),),
        )
        with SqliteSession(index.facts_path) as formal:
            parent = ReadCompanyPublicationIdentity(formal)
        SQLiteCompanyFactsIndexFixture(index.facts_path).publish(
            (legacy,), {item.identity: item.source}, parent, NOW
        )
    assert _facts(index).risk_events[0].reduction_plan.planned_ratio_pct is None
    assert not tuple((tmp_path / "text").rglob("*.gz"))
    return index, item


def test_legacy_reduction_repairs_only_its_missing_field_and_stays_complete_after_restart(tmp_path):
    index, item = _legacy_reduction(tmp_path)
    index.prepare_publication(item.codes, NOW)
    pending = index.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES)
    assert [task.disclosure.identity for task in pending] == [item.identity]
    assert [tuple(row) for row in _state(index)] == [("planned_pct", "pending")]
    with SqliteSession(index.facts_path) as formal:
        original_receipt = tuple(formal.run(
            "SELECT record_hash,extraction_hash,outcome FROM company_input_receipt WHERE outcome='valid'"
        ).require_one())

    assert index.commit_document(item, fixtures._extraction(_PLAN), NOW)
    index.publish(item.codes, NOW)
    assert _facts(index).risk_events[0].reduction_plan.planned_ratio_pct == 2
    assert [tuple(row) for row in _state(index)] == [("planned_pct", "complete")]
    assert not tuple((tmp_path / "text").rglob("*.gz"))
    with SqliteSession(index.facts_path) as formal:
        assert tuple(formal.run(
            "SELECT record_hash,extraction_hash,outcome FROM company_input_receipt WHERE outcome='valid'"
        ).require_one()) == original_receipt
        assert formal.run(
            "SELECT COUNT(*) FROM company_input_receipt WHERE outcome='field_attempted'"
        ).require_one()[0] == 1

    for turn in range(1, 4):
        restarted = fixtures._index(tmp_path)
        at = NOW + timedelta(minutes=turn)
        restarted.prepare_publication(item.codes, at)
        assert restarted.pending_documents(item.codes, at, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()
        assert [tuple(row) for row in _state(restarted)] == [("planned_pct", "complete")]


def test_same_rule_missing_measurement_is_attempted_once_and_retains_local_text(tmp_path):
    index, item = _legacy_reduction(tmp_path, _PLAN_WITHOUT_RATIO)
    index.prepare_publication(item.codes, NOW)
    assert index.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES)
    assert index.commit_document(item, fixtures._extraction(_PLAN_WITHOUT_RATIO), NOW)
    index.publish(item.codes, NOW)
    assert _facts(index).risk_events[0].reduction_plan.planned_ratio_pct is None
    assert [tuple(row) for row in _state(index)] == [("planned_pct", "attempted")]
    assert tuple((tmp_path / "text").rglob("*.gz"))

    restarted = fixtures._index(tmp_path)
    restarted.prepare_publication(item.codes, NOW + timedelta(minutes=1))
    assert restarted.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal:
        plans = ReadCompanyFieldRecovery(formal, collected, item.codes, NOW, tmp_path)
    planned_ratio = next(plan for plan in plans if plan.gap.field == "planned_pct")
    assert (planned_ratio.action, planned_ratio.reason) == ("verify", "same_rule_already_attempted")
    assert tuple((tmp_path / "text").rglob("*.gz"))


def test_formal_field_receipt_recovers_collection_rollback_without_another_document_request(tmp_path):
    index, item = _legacy_reduction(tmp_path)
    index.prepare_publication(item.codes, NOW)
    assert index.commit_document(item, fixtures._extraction(_PLAN), NOW)
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run(
            "CREATE TRIGGER fail_after_formal_commit BEFORE INSERT ON document_publications "
            "BEGIN SELECT RAISE(ABORT,'collection_commit_crash'); END"
        )
    with pytest.raises(RuntimeError, match="company_evidence_persistence_unavailable"):
        index.publish(item.codes, NOW)
    assert _facts(index).risk_events[0].reduction_plan.planned_ratio_pct == 2
    assert [tuple(row) for row in _state(index)] == [("planned_pct", "pending")]
    with SqliteSession(index.facts_path) as formal:
        assert formal.run(
            "SELECT COUNT(*) FROM company_input_receipt WHERE outcome='field_attempted'"
        ).require_one()[0] == 1
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("DROP TRIGGER fail_after_formal_commit")

    restarted = fixtures._index(tmp_path)
    restarted.prepare_publication(item.codes, NOW + timedelta(minutes=1))
    assert [tuple(row) for row in _state(restarted)] == [("planned_pct", "complete")]
    assert restarted.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()
    assert not tuple((tmp_path / "text").rglob("*.gz"))


def test_document_selection_prefers_latest_date_but_local_only_can_recover_older_pdf(tmp_path):
    index = fixtures._index(tmp_path)
    older = fixtures._disclosure("older-plan", "控股股东减持计划公告", when=NOW - timedelta(days=20))
    latest = fixtures._disclosure("latest-plan", "控股股东减持计划公告")
    for item in (older, latest):
        partition = DisclosurePartition("sse", item.published_at.date(), item.published_at.date())
        index.commit_page(partition, DisclosurePage((item,), 1, False), NOW)
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run(
            "INSERT INTO document_tasks VALUES(?,?,?,'downloaded',?,?,?)",
            (older.source, older.identity, older.digest, "documents/older.pdf", bytes(32), 10),
        )
    latest_task = index.pending_documents(
        latest.codes, NOW, limit=1, policies=fixtures._TEST_DOCUMENT_POLICIES
    )
    assert [task.disclosure.identity for task in latest_task] == [latest.identity]
    local_task = index.pending_documents(
        latest.codes, NOW, limit=1, policies=fixtures._TEST_DOCUMENT_POLICIES, local_only=True
    )
    assert [task.disclosure.identity for task in local_task] == [older.identity]


def test_receipt_restore_keeps_missing_event_field_open_when_evidence_changes_and_other_task_is_pending(tmp_path):
    index, item = _legacy_reduction(tmp_path, _PLAN_WITHOUT_RATIO)
    index.prepare_publication(item.codes, NOW)
    assert index.commit_document(item, fixtures._extraction(_PLAN_WITHOUT_RATIO), NOW)
    facts = _facts(index)
    event = facts.risk_events[0]
    changed_evidence = replace(facts, risk_events=(replace(event, evidence_ids=("updated-source",)),))
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal:
        receipts = CompanyFieldRepairReceipts(collected, item.codes, NOW)
        parent = ReadCompanyPublicationIdentity(formal)
    assert len(receipts) == 1
    SQLiteCompanyFactsIndexFixture(index.facts_path).publish(
        (changed_evidence,), {"updated-source": item.source}, parent, NOW, receipts=receipts
    )
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run(
            "INSERT INTO company_field_repairs "
            "SELECT disclosure_id,code,record_hash,'other-event',field,rule_id,'pending' "
            "FROM company_field_repairs WHERE event_id=?",
            (event.event_id,),
        )
    with (
        SqliteSession(index.path) as collected,
        SqliteSession(index.facts_path) as formal,
        SqliteTransaction(collected),
    ):
        RestoreCompanyFieldRepairs(collected, formal)
        states = {row[0]: row[1] for row in collected.run(
            "SELECT event_id,state FROM company_field_repairs"
        )}
    assert states == {event.event_id: "attempted", "other-event": "pending"}
