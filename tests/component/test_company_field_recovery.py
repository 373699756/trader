"""Field-rule repairs resume through the existing publication and collection owners."""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from tests.component import test_company_evidence as fixtures
from tests.helpers.configuration import SQLiteCompanyEvidenceReaderFixture, SQLiteCompanyFactsIndexFixture
from trader.download.domain.company_evidence import DisclosurePage, DisclosurePartition
from trader.download.domain.company_events import (
    CompanyEventStatus,
    CompanyRiskEvent,
    CompanyRiskType,
    CompanySeverityBasis,
    CompanySubject,
    ReductionPlan,
)
from trader.download.infra.company_facts_index import CompanyInputReceipt, ReadCompanyPublicationIdentity
from trader.download.infra.company_field_recovery import (
    CompanyFieldRepairReceipts,
    ReadCompanyFieldRecovery,
    RegisterCompanyFieldAttempts,
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


def test_first_missing_measurement_is_checkpointed_and_retained_without_second_download(tmp_path):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("first-plan", "控股股东减持计划公告")
    _put(index, item, _PLAN_WITHOUT_RATIO)
    index.publish(item.codes, NOW)
    assert _facts(index).risk_events[0].reduction_plan.planned_ratio_pct is None
    assert [tuple(row) for row in _state(index)] == [("planned_pct", "attempted")]
    with SqliteSession(index.facts_path) as formal:
        receipts = tuple(tuple(row) for row in formal.run(
            "SELECT * FROM company_input_receipt WHERE outcome='field_attempted'"
        ))
    assert len(receipts) == 1
    assert tuple((tmp_path / "text").rglob("*.gz"))
    for turn in range(1, 4):
        restarted = fixtures._index(tmp_path)
        at = NOW + timedelta(minutes=turn)
        restarted.prepare_publication(item.codes, at)
        assert restarted.pending_documents(item.codes, at, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()
        assert [tuple(row) for row in _state(restarted)] == [("planned_pct", "attempted")]
        with SqliteSession(index.facts_path) as formal:
            assert tuple(tuple(row) for row in formal.run(
                "SELECT * FROM company_input_receipt WHERE outcome='field_attempted'"
            )) == receipts
        assert tuple((tmp_path / "text").rglob("*.gz"))


def test_first_contract_with_amount_but_missing_dependencies_retains_evidence_without_redownload(tmp_path):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("first-contract", "关于签订重大合同的公告")
    _put(index, item, "本公司已签订正式合同，合同已生效。合同金额人民币2亿元（不含税）。" + _DETAIL)
    index.publish(item.codes, NOW)
    measurements = _facts(index).positive_events[0].measurements
    assert measurements.contract_amount_cny == 200_000_000
    assert measurements.reference_annual_revenue_cny is None
    states = tuple(tuple(row) for row in _state(index))
    assert ("annual_revenue", "attempted") in states
    assert ("contract_amount", "attempted") not in states
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal:
        rules = tuple(tuple(row) for row in collected.run("SELECT field,rule_id FROM company_field_repairs"))
        assert dict(rules)["revenue_report_date"] == "explicit_contract_revenue_year:1"
        assert all(rule == "required_field_evidence:1" for field, rule in rules if field != "revenue_report_date")
        assert formal.run(
            "SELECT COUNT(*) FROM company_input_receipt WHERE outcome='field_attempted'"
        ).require_one()[0] == len(states)
    assert tuple((tmp_path / "text").rglob("*.gz"))
    restarted = fixtures._index(tmp_path)
    restarted.prepare_publication(item.codes, NOW + timedelta(minutes=1))
    assert restarted.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal:
        plans = ReadCompanyFieldRecovery(formal, collected, item.codes, NOW, tmp_path)
    annual = next(plan for plan in plans if plan.gap.field == "annual_revenue")
    assert (annual.action, annual.reason) == ("verify", "dependency_evidence_required")
    assert tuple((tmp_path / "text").rglob("*.gz"))


def test_first_field_task_and_receipt_roll_back_when_formal_publication_fails(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("uncommitted-plan", "控股股东减持计划公告")
    _put(index, item, _PLAN_WITHOUT_RATIO)

    def unavailable(*args, **kwargs):
        raise RuntimeError("fixture_formal_publication_failed")

    with monkeypatch.context() as patch:
        patch.setattr(index._facts, "publish", unavailable)
        with pytest.raises(RuntimeError, match="fixture_formal_publication_failed"):
            index.publish(item.codes, NOW)
    assert _state(index) == ()
    with SqliteSession(index.facts_path) as formal:
        assert formal.run("SELECT COUNT(*) FROM company_facts").require_one()[0] == 0
        assert formal.run("SELECT COUNT(*) FROM company_input_receipt").require_one()[0] == 0
    assert tuple((tmp_path / "text").rglob("*.gz"))
    index.publish(item.codes, NOW)
    assert [tuple(row) for row in _state(index)] == [("planned_pct", "attempted")]


def test_first_field_receipt_recovers_task_lost_in_collection_rollback(tmp_path):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("first-crash-plan", "控股股东减持计划公告")
    _put(index, item, _PLAN_WITHOUT_RATIO)
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run(
            "CREATE TRIGGER fail_first_collection_commit BEFORE INSERT ON document_publications "
            "BEGIN SELECT RAISE(ABORT,'first_collection_commit_crash'); END"
        )
    with pytest.raises(RuntimeError, match="company_evidence_persistence_unavailable"):
        index.publish(item.codes, NOW)
    assert _state(index) == ()
    assert _facts(index).risk_events[0].reduction_plan.planned_ratio_pct is None
    with SqliteSession(index.facts_path) as formal:
        assert formal.run(
            "SELECT COUNT(*) FROM company_input_receipt WHERE outcome='field_attempted'"
        ).require_one()[0] == 1
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("DROP TRIGGER fail_first_collection_commit")
    restarted = fixtures._index(tmp_path)
    restarted.prepare_publication(item.codes, NOW + timedelta(minutes=1))
    assert [tuple(row) for row in _state(restarted)] == [("planned_pct", "attempted")]
    assert restarted.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()
    assert tuple((tmp_path / "text").rglob("*.gz"))


@pytest.mark.parametrize("mismatch", ("source", "code", "record_hash", "body_hash", "extraction_hash"))
def test_field_attempt_requires_the_accepted_source_version_and_body_hash(tmp_path, mismatch):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("bound-plan", "控股股东减持计划公告")
    _put(index, item, _PLAN_WITHOUT_RATIO)
    index.publish(item.codes, NOW)
    facts = _facts(index)
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        body_hash = collected.run("SELECT body_hash FROM disclosures WHERE source_key=?", (item.identity,)).require_one()[0]
        collected.run("DELETE FROM company_field_repairs")
        receipt = CompanyInputReceipt(
            item.source, item.identity, item.codes[0], item.digest, index._facts.extraction_hash, body_hash, NOW
        )
        if mismatch == "source":
            changed = replace(receipt, source="different")
        elif mismatch == "code":
            changed = replace(receipt, code="600002")
        elif mismatch == "record_hash":
            changed = replace(receipt, record_hash="different")
        elif mismatch == "body_hash":
            changed = replace(receipt, body_hash=bytes(32))
        else:
            changed = replace(receipt, extraction_hash=bytes(32))
        RegisterCompanyFieldAttempts(collected, (facts,), (changed,), index._facts.extraction_hash)
        assert collected.run("SELECT COUNT(*) FROM company_field_repairs").require_one()[0] == 0


@pytest.mark.parametrize("mismatch", ("record_hash", "body_hash"))
def test_first_attempt_restore_rejects_a_different_current_source_or_body(tmp_path, mismatch):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("changed-plan", "控股股东减持计划公告")
    _put(index, item, _PLAN_WITHOUT_RATIO)
    index.publish(item.codes, NOW)
    with (
        SqliteSession(index.path) as collected,
        SqliteSession(index.facts_path) as formal,
        SqliteTransaction(collected),
    ):
        collected.run("DELETE FROM company_field_repairs")
        if mismatch == "record_hash":
            collected.run("UPDATE disclosures SET record_hash='changed' WHERE source_key=?", (item.identity,))
        else:
            collected.run("UPDATE disclosures SET body_hash=? WHERE source_key=?", (bytes(32), item.identity))
        RestoreCompanyFieldRepairs(collected, formal)
        assert collected.run("SELECT COUNT(*) FROM company_field_repairs").require_one()[0] == 0


def test_recovery_without_an_automatic_field_rule_is_explicitly_unsupported(tmp_path):
    index, item = _legacy_reduction(tmp_path)
    facts = _facts(index)
    event = facts.risk_events[0]
    legacy = replace(facts, risk_events=(replace(
        event, reduction_plan=replace(event.reduction_plan, planned_start_date=None)
    ),))
    with SqliteSession(index.facts_path) as formal:
        parent = ReadCompanyPublicationIdentity(formal)
    SQLiteCompanyFactsIndexFixture(index.facts_path).publish(
        (legacy,), {item.identity: item.source}, parent, NOW
    )
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal:
        plans = ReadCompanyFieldRecovery(formal, collected, item.codes, NOW, tmp_path)
    start = next(plan for plan in plans if plan.gap.field == "start_date")
    assert (start.action, start.reason) == ("unsupported", "automatic_field_rule_unavailable")


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


def test_existing_subject_exclusions_are_removed_from_legacy_queue_without_fake_processing(tmp_path):
    from trader.download.infra.company_facts_protocol import COMPANY_EXTRACTION_IDENTITY

    index = fixtures._index(tmp_path)
    items = (
        fixtures._disclosure("director", "关于董事减持股份计划的公告"),
        fixtures._disclosure("subsidiary", "关于子公司签订重大合同的公告"),
        fixtures._disclosure("holder-director", "关于控股股东兼董事减持计划的公告"),
        fixtures._disclosure("issuer-board", "关于董事会审议回购股份方案的公告"),
    )
    partition = DisclosurePartition("sse", NOW.date(), NOW.date())
    index.commit_page(partition, DisclosurePage(items, 4, False), NOW)
    index.publish(items[0].codes, NOW)
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("INSERT OR IGNORE INTO company_document_requirements SELECT id FROM disclosures")
        collected.run("UPDATE company_work_policy SET work_policy_id=?", (COMPANY_EXTRACTION_IDENTITY,))
    restarted = fixtures._index(tmp_path)
    restarted.prepare_publication(items[0].codes, NOW)
    tasks = restarted.pending_documents(items[0].codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES)
    assert {task.disclosure.identity for task in tasks} == {"holder-director", "issuer-board"}
    assert not restarted.document_required(items[0])
    assert not restarted.document_required(items[1])
    restarted.publish(items[0].codes, NOW, changed_only=True)
    assert _facts(restarted).pending_document_count == 2
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal:
        assert collected.run("SELECT COUNT(*) FROM disclosures WHERE body_checked=0").require_one()[0] == 4
        assert collected.run("SELECT COUNT(*) FROM company_document_exclusions").require_one()[0] == 2
        assert collected.run("SELECT COUNT(*) FROM document_publications").require_one()[0] == 0
        assert collected.run("SELECT COUNT(*) FROM document_extractions").require_one()[0] == 0
        assert formal.run("SELECT COUNT(*) FROM company_input_receipt").require_one()[0] == 0


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


def test_latest_value_retires_older_queued_repair_without_download_or_new_document_receipt(tmp_path):
    index, item = _legacy_reduction(tmp_path)
    index.prepare_publication(item.codes, NOW)
    assert index.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES)
    with SqliteSession(index.facts_path) as formal:
        original = tuple(tuple(row) for row in formal.run("SELECT * FROM company_input_receipt"))
    at = NOW + timedelta(minutes=1)
    latest = fixtures._disclosure("latest-plan", "控股股东减持计划补充公告", when=at)
    index.commit_page(DisclosurePartition("sse", at.date(), at.date()), DisclosurePage((latest,), 1, False), at)
    assert index.commit_document(latest, fixtures._extraction("本公司控股股东就original-plan的减持计划补充披露。" + _PLAN), at)
    index.publish(item.codes, at, changed_only=True)
    assert [(event.event_id, event.reduction_plan.planned_ratio_pct) for event in _facts(index).risk_events] == [
        ("legacy-reduction", 2),
    ]
    assert _facts(index).pending_document_count == 0
    assert [tuple(row) for row in _state(index)] == [("planned_pct", "complete")]
    assert index.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()
    with SqliteSession(index.facts_path) as formal:
        assert tuple(tuple(row) for row in formal.run(
            "SELECT * FROM company_input_receipt WHERE source_key=?", (item.identity,),
        )) == original
        assert formal.run("SELECT COUNT(*) FROM company_input_receipt WHERE outcome='field_attempted'").require_one()[0] == 0
    with SqliteSession(index.path) as collected:
        assert collected.run(
            "SELECT COUNT(*) FROM document_extractions x JOIN disclosures d ON d.id=x.disclosure_id WHERE d.source_key=?",
            (item.identity,),
        ).require_one()[0] == 0
    for turn in range(2, 5):
        restarted = fixtures._index(tmp_path)
        restarted.prepare_publication(item.codes, NOW + timedelta(minutes=turn))
        assert restarted.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()


def test_restart_retires_old_repair_after_latest_formal_commit_before_collection_commit(tmp_path):
    index, item = _legacy_reduction(tmp_path)
    index.prepare_publication(item.codes, NOW)
    _publish_latest_ratio(index, item)
    restarted = fixtures._index(tmp_path)
    restarted.prepare_publication(item.codes, NOW + timedelta(minutes=2))
    assert [tuple(row) for row in _state(restarted)] == [("planned_pct", "complete")]
    assert restarted.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()


@pytest.mark.parametrize("guard", ["different_event", "changed_source", "changed_body", "changed_rule", "missing_receipt", "other_field", "other_code"])
def test_latest_field_does_not_skip_unverified_versions_or_other_requirements(tmp_path, guard):
    index, item = _legacy_reduction(tmp_path)
    index.prepare_publication(item.codes, NOW)
    _publish_latest_ratio(index, item, different_event=guard == "different_event")
    if guard in {"changed_source", "changed_body", "other_field", "other_code"}:
        with SqliteSession(index.path) as collected, SqliteTransaction(collected):
            if guard == "changed_source":
                collected.run("UPDATE disclosures SET record_hash='changed-source',body_hash=NULL")
            elif guard == "changed_body":
                collected.run("UPDATE disclosures SET body_hash=?", (bytes(32),))
            elif guard == "other_field":
                collected.run(
                    "INSERT INTO company_field_repairs SELECT disclosure_id,code,record_hash,event_id,"
                    "'completion_disclosed_at',rule_id,'pending' FROM company_field_repairs"
                )
            else:
                collected.run("INSERT INTO company_population VALUES('600002')")
                collected.run("INSERT INTO disclosure_codes SELECT '600002',id FROM disclosures")
                collected.run(
                    "INSERT INTO company_field_repairs SELECT disclosure_id,'600002',record_hash,event_id,"
                    "field,rule_id,'pending' FROM company_field_repairs"
                )
    if guard in {"changed_rule", "missing_receipt"}:
        # The fixture publisher updates the publication identity along with the
        # receipt mutation so the normal checksum gate remains in force.
        formal_index = SQLiteCompanyFactsIndexFixture(index.facts_path)
        if guard == "missing_receipt":
            with SqliteSession(index.facts_path) as formal, SqliteTransaction(formal):
                formal.run("DELETE FROM company_input_receipt")
        else:
            with SqliteSession(index.facts_path) as formal, SqliteTransaction(formal):
                formal.run("UPDATE company_input_receipt SET extraction_hash=?", (bytes(32),))
        formal_index.publish_file()

    index.publish(item.codes, NOW + timedelta(minutes=1), changed_only=True)
    assert index.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES)
    with SqliteSession(index.path) as collected:
        assert collected.run("SELECT body_checked FROM disclosures").require_one()[0] == 0
    restarted = fixtures._index(tmp_path)
    codes = (*item.codes, "600002") if guard == "other_code" else item.codes
    restarted.prepare_publication(codes, NOW + timedelta(minutes=2))
    assert restarted.pending_documents(codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES)


@pytest.mark.parametrize("missing_event", (False, True))
def test_receipt_restore_keeps_missing_event_field_open_when_evidence_changes_and_other_task_is_pending(tmp_path, missing_event):
    index, item = _legacy_reduction(tmp_path, _PLAN_WITHOUT_RATIO)
    index.prepare_publication(item.codes, NOW)
    assert index.commit_document(item, fixtures._extraction(_PLAN_WITHOUT_RATIO), NOW)
    facts = _facts(index)
    event = facts.risk_events[0]
    changed_evidence = replace(facts, risk_events=() if missing_event else (replace(event, evidence_ids=("updated-source",)),))
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
