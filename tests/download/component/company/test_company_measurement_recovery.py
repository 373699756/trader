"""Synthetic gap fixtures exercise local recovery and real formal-reader round trips."""

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company.company_field_recovery import NOW, _facts, _legacy_reduction
from tests.helpers.download.company_resources import CompanyDocumentFilesFixture, SQLiteCompanyEvidenceReaderFixture, SQLiteCompanyFactsIndexFixture
from trader.download.domain.company_evidence import DisclosurePage, DisclosurePartition
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.download.infra.company_field_recovery import ReadCompanyFieldRecovery
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction


_DETAIL = "本公告仅说明本次事项的既有安排。相关信息请以本次正式披露为准。本公司将按照规定及时披露后续进展情况。"


def _valid_receipts(index):
    with SqliteSession(index.facts_path) as connection:
        return tuple(tuple(row) for row in connection.run(
            "SELECT * FROM company_input_receipt WHERE outcome='valid' ORDER BY source,source_key,code"
        ))


def _attempt_receipts(index, field, rule):
    with SqliteSession(index.facts_path) as connection:
        return tuple(tuple(row) for row in connection.run(
            "SELECT * FROM company_input_receipt WHERE outcome='field_attempted' AND policy_id LIKE ?",
            (f"%:{field}:{rule}",),
        ))


def _old_attempt(index, item, event_id, field, rule):
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run(
            "INSERT OR REPLACE INTO company_field_repairs "
            "SELECT id,?,record_hash,?,?,?,'attempted' FROM disclosures WHERE source_key=?",
            (item.codes[0], event_id, field, rule, item.identity),
        )


@pytest.mark.parametrize(("body", "ratio"), (
    ("本公司控股股东广晟集团计划以集中竞价方式减持公司股份，占公司总股本的比例不超过0.60%。", 0.6),
    ("本公司控股股东兴泰集团计划减持不超过本公司总股本的1%。", 1),
))
def test_changed_plan_rule_reuses_local_text_once_and_preserves_ordinary_valid(tmp_path, body, ratio):
    text = body + _DETAIL
    index, item = _legacy_reduction(tmp_path, text)
    before = _facts(index).risk_events[0]
    documents = CompanyDocumentFilesFixture(tmp_path / "text")
    body_hash = documents.write(text)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE disclosures SET body_retained=1,body_hash=?", (body_hash,))
    _old_attempt(index, item, before.event_id, "planned_pct", "reduction_total_capital_ratio:1")
    ordinary = _valid_receipts(index)
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal:
        plan, = ReadCompanyFieldRecovery(formal, collected, item.codes, NOW, tmp_path)
    assert (plan.action, plan.reason) == ("extract", "local_text_available")

    index.prepare_publication(item.codes, NOW + timedelta(minutes=1))
    reader = SQLiteCompanyEvidenceReaderFixture(index.facts_path)
    actual = reader.read(item.codes)[0].risk_events[0]
    assert actual.reduction_plan.planned_ratio_pct == ratio
    assert actual.event_id == before.event_id and actual.subject_type == before.subject_type
    assert actual.reduction_plan.planned_start_date == before.reduction_plan.planned_start_date
    assert actual.reduction_plan.planned_end_date == before.reduction_plan.planned_end_date
    assert actual.expires_at.isoformat() == "2027-02-17T23:59:59.999999+08:00"
    field = next(result for result in reader.read_fields(item.codes).results if result.field == "planned_pct")
    assert (field.value, field.state, field.evidence) == (ratio, "present_unverified", ())
    assert _valid_receipts(index) == ordinary
    attempted = _attempt_receipts(index, "planned_pct", "reduction_total_capital_ratio:2")
    assert len(attempted) == 1

    restarted = fixtures._index(tmp_path)
    restarted.prepare_publication(item.codes, NOW + timedelta(minutes=2))
    assert restarted.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()
    assert _attempt_receipts(restarted, "planned_pct", "reduction_total_capital_ratio:2") == attempted
    assert _valid_receipts(restarted) == ordinary
    assert SQLiteCompanyEvidenceReaderFixture(restarted.facts_path).read(item.codes)[0].risk_events[0] == actual


def test_changed_cny_amount_rule_recovers_local_text_once_without_inventing_basis_or_binding(tmp_path):
    text = "本公司拟签订经营合同。合同额：人民币3340000.00元。" + _DETAIL
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("amount-wording", "关于签订经营合同的公告")
    partition = DisclosurePartition("sse", NOW.date(), NOW.date())
    index.commit_page(partition, DisclosurePage((item,), 1, False), NOW)
    assert index.commit_document(item, fixtures._extraction(text), NOW)
    index.publish(item.codes, NOW)
    facts = _facts(index)
    event, = facts.positive_events
    legacy = replace(facts, positive_events=(replace(
        event, measurements=replace(event.measurements, contract_amount_cny=None),
    ),))
    writer = SQLiteCompanyFactsIndexFixture(index.facts_path)
    with SqliteSession(index.facts_path) as connection:
        parent = ReadCompanyPublicationIdentity(connection)
    writer.publish((legacy,), {item.identity: item.source}, parent, NOW + timedelta(seconds=1))
    _old_attempt(index, item, event.event_id, "contract_amount", "explicit_cny_contract_amount:2")
    ordinary = _valid_receipts(index)

    index.prepare_publication(item.codes, NOW + timedelta(minutes=1))
    reader = SQLiteCompanyEvidenceReaderFixture(index.facts_path)
    actual, = reader.read(item.codes)[0].positive_events
    assert actual.measurements.contract_amount_cny == 3_340_000
    assert actual.measurements.reference_annual_revenue_cny is None
    assert actual.measurements.amount_to_revenue_pct is None
    assert actual.measurements.amount_basis == "unknown"
    assert not actual.measurements.binding_confirmed
    assert actual.status == event.status and actual.event_id == event.event_id
    assert actual.positive_level == 0 and actual.level_milestones == ()
    field = next(result for result in reader.read_fields(item.codes).results if result.field == "contract_amount")
    assert (field.value, field.state, field.evidence) == (3_340_000, "present_unverified", ())
    assert _valid_receipts(index) == ordinary
    attempted = _attempt_receipts(index, "contract_amount", "explicit_cny_contract_amount:3")
    assert len(attempted) == 1

    restarted = fixtures._index(tmp_path)
    restarted.prepare_publication(item.codes, NOW + timedelta(minutes=2))
    assert restarted.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()
    assert _attempt_receipts(restarted, "contract_amount", "explicit_cny_contract_amount:3") == attempted
    assert _valid_receipts(restarted) == ordinary
    assert SQLiteCompanyEvidenceReaderFixture(restarted.facts_path).read(item.codes)[0].positive_events == (actual,)
