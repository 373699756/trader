from dataclasses import replace

from datetime import date, timedelta

import pytest

from tests.helpers.download.company import company_evidence as fixtures

from tests.helpers.download.company_resources import (
    CompanyDocumentFilesFixture, SQLiteCompanyEvidenceReaderFixture, SQLiteCompanyFactsIndexFixture,
)


from trader.download.domain.company_evidence import DisclosurePage, DisclosurePartition

from trader.download.domain.company_events import CompanyPositiveStatus, CompanyReviewReason, CompanySubject, RepurchasePurpose

from trader.download.infra.company_facts_index import CompanyInputReceipt, ReadCompanyPublicationIdentity

from trader.download.infra.company_field_recovery import (
    CompanyFieldRepairReceipts,
    QueueCompanyRepurchaseRechecks,
    ReadCompanyFieldRecovery,
    RegisterCompanyFieldAttempts,
    RestoreCompanyFieldRepairs,
)

from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction

from tests.helpers.download.company.company_field_recovery import (
    NOW,
    _DETAIL,
    _PLAN,
    _PLAN_WITHOUT_RATIO,
    _facts,
    _legacy_reduction,
    _legacy_repurchase,
    _legacy_repurchase_template,
    _legacy_holder_table,
    _publish_latest_ratio,
    _put,
    _state,
)

from trader.download.infra.company_link_codec import DecodeCompanyLink, EncodeCompanyLink
from trader.download.infra.company_publication_reads import ReadCompanyPublicationLinks


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
    assert not tuple((tmp_path / "text").rglob("*.gz"))  # All applicable fields now have published source proofs.
    with SqliteSession(index.facts_path) as formal:
        assert formal.run("SELECT state FROM company_field_result WHERE field='planned_pct'").require_one()[0] == "verified"
    with SqliteSession(index.facts_path) as formal:
        assert tuple(formal.run(
            "SELECT record_hash,extraction_hash,outcome FROM company_input_receipt WHERE outcome='valid'"
        ).require_one()) == original_receipt
        assert formal.run(
            "SELECT COUNT(*) FROM company_input_receipt WHERE outcome='field_attempted' AND policy_id LIKE 'field:%'"
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
    assert len(receipts) == 3  # One missing-field attempt and two explicit date proofs.
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
            "SELECT COUNT(*) FROM company_input_receipt WHERE outcome='field_attempted' AND policy_id LIKE 'field:%'"
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


def test_existing_reduction_date_rules_recover_old_generic_tasks_from_local_text(tmp_path):
    index, item = _legacy_reduction(tmp_path)
    facts = _facts(index)
    event = facts.risk_events[0]
    legacy = replace(facts, risk_events=(replace(
        event, reduction_plan=replace(event.reduction_plan, planned_start_date=None, planned_end_date=None)
    ),))
    body_hash = CompanyDocumentFilesFixture(tmp_path / "text").write(fixtures._extraction(_PLAN).text)
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("UPDATE disclosures SET body_retained=1,body_hash=?", (body_hash,))
        for field in ("start_date", "end_date"):
            collected.run(
                "INSERT INTO company_field_repairs SELECT id,?,record_hash,?,?,"
                "'required_field_evidence:1','pending' FROM disclosures WHERE source_key=?",
                (item.codes[0], event.event_id, field, item.identity),
            )
        old_attempts = CompanyFieldRepairReceipts(collected, item.codes, NOW)
    with SqliteSession(index.facts_path) as formal:
        parent = ReadCompanyPublicationIdentity(formal)
    SQLiteCompanyFactsIndexFixture(index.facts_path).publish(
        (legacy,), {item.identity: item.source}, parent, NOW, receipts=old_attempts
    )
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("UPDATE company_field_repairs SET state='attempted'")
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal:
        valid = tuple(tuple(row) for row in formal.run("SELECT * FROM company_input_receipt WHERE outcome='valid'"))
        plans = ReadCompanyFieldRecovery(formal, collected, item.codes, NOW, tmp_path)
    assert all(plan.action == "extract" for plan in plans if plan.gap.field in {"start_date", "end_date"})
    index.prepare_publication(item.codes, NOW)
    restored = _facts(index)
    assert len(restored.risk_events) == 1 and restored.risk_events[0].event_id == event.event_id
    assert restored.risk_events[0].reduction_plan.planned_start_date == date(2026, 10, 19)
    assert restored.risk_events[0].reduction_plan.planned_end_date == date(2026, 11, 19)
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal:
        rows = tuple(tuple(row) for row in collected.run(
            "SELECT field,rule_id,state FROM company_field_repairs WHERE field IN ('start_date','end_date')"
        ))
        assert len(rows) == 4 and all(row[2] == "complete" for row in rows)
        assert {(row[0], row[1]) for row in rows if row[1] != "required_field_evidence:1"} == {
            ("start_date", "explicit_reduction_start_date:2"), ("end_date", "explicit_reduction_end_date:2"),
        }
        assert tuple(tuple(row) for row in formal.run("SELECT * FROM company_input_receipt WHERE outcome='valid'")) == valid


def test_repurchase_purpose_rule_preserves_planned_state_and_other_event_requirements(tmp_path):
    index, item = _legacy_repurchase(tmp_path, "本公司拟回购股份，回购股份用于注销。" + _DETAIL)
    planned = _facts(index).positive_events[0]
    other = fixtures._disclosure("other-repurchase", "关于回购股份的公告", when=NOW - timedelta(days=1))
    _put(index, other, "本公司累计已实际回购股份，占公司总股本的比例为0.2%。" + _DETAIL)
    index.publish(item.codes, NOW)
    with SqliteSession(index.facts_path) as formal:
        valid = tuple(tuple(row) for row in formal.run(
            "SELECT * FROM company_input_receipt WHERE source_key=? AND outcome='valid'", (item.identity,),
        ))

    index.prepare_publication(item.codes, NOW)

    facts = _facts(index)
    assert len(facts.positive_events) == 2
    restored = next(event for event in facts.positive_events if event.event_id == planned.event_id)
    untouched = next(event for event in facts.positive_events if event.event_id != planned.event_id)
    assert restored.measurements.purpose is RepurchasePurpose.CANCELLATION
    assert restored.status is CompanyPositiveStatus.PLANNED and restored.measurements.actual_ratio_pct is None
    assert restored.positive_level == 0 and restored.level_milestones == ()
    assert untouched.measurements.purpose is RepurchasePurpose.UNKNOWN
    assert untouched.measurements.actual_ratio_pct == 0.2
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal:
        assert collected.run(
            "SELECT state FROM company_field_repairs WHERE event_id=? AND field='repurchase_purpose' "
            "AND rule_id='explicit_repurchase_purpose:2'", (planned.event_id,),
        ).require_one()[0] == "complete"
        assert collected.run(
            "SELECT state FROM company_field_repairs WHERE event_id=? AND field='repurchase_purpose' "
            "AND rule_id='explicit_repurchase_purpose:2'", (untouched.event_id,),
        ).require_one()[0] == "attempted"
        assert collected.run(
            "SELECT count(*) FROM company_field_repairs WHERE event_id=? AND field='actual_pct'", (planned.event_id,),
        ).require_one()[0] == 0
        assert tuple(tuple(row) for row in formal.run(
            "SELECT * FROM company_input_receipt WHERE source_key=? AND outcome='valid'", (item.identity,),
        )) == valid


def test_unmatched_repurchase_purpose_is_attempted_once_then_verified_without_redownload(tmp_path):
    index, item = _legacy_repurchase(
        tmp_path, "本公司累计已实际回购股份，占公司总股本的比例为0.2%。" + _DETAIL,
    )
    with SqliteSession(index.facts_path) as formal:
        valid = tuple(tuple(row) for row in formal.run("SELECT * FROM company_input_receipt WHERE outcome='valid'"))
    index.prepare_publication(item.codes, NOW)
    assert _facts(index).positive_events[0].measurements.purpose is RepurchasePurpose.UNKNOWN
    with SqliteSession(index.facts_path) as formal:
        attempts = tuple(tuple(row) for row in formal.run(
            "SELECT * FROM company_input_receipt WHERE outcome='field_attempted' ORDER BY policy_id",
        ))
    assert len(tuple(row for row in attempts if not row[-1].startswith("field_source_lines:"))) == 2
    assert len(tuple(row for row in attempts if row[-1].startswith("field_source_lines:"))) == 1
    for _ in range(2):
        restarted = fixtures._index(tmp_path)
        restarted.prepare_publication(item.codes, NOW)
        assert not restarted.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES)
        with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal:
            purpose = next(plan for plan in ReadCompanyFieldRecovery(formal, collected, item.codes, NOW, tmp_path)
                           if plan.gap.field == "repurchase_purpose")
            assert (purpose.action, purpose.reason) == ("verify", "same_rule_already_attempted")
            assert tuple(tuple(row) for row in formal.run(
                "SELECT * FROM company_input_receipt WHERE outcome='field_attempted' ORDER BY policy_id",
            )) == attempts
            assert tuple(tuple(row) for row in formal.run("SELECT * FROM company_input_receipt WHERE outcome='valid'")) == valid
            assert collected.run(
                "SELECT state FROM company_field_repairs WHERE field='repurchase_purpose' AND rule_id='explicit_repurchase_purpose:2'"
            ).require_one()[0] == "attempted"


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
        plans = ReadCompanyFieldRecovery(formal, collected, item.codes, NOW, tmp_path)
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
        plans = ReadCompanyFieldRecovery(formal, collected, ("600001", "600002"), NOW, tmp_path)
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
        plans = ReadCompanyFieldRecovery(formal, collected, ("600001", "600002"), NOW, tmp_path)
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
            "'reduction_total_capital_ratio:3','attempted' FROM disclosures WHERE source='exchange_disclosure'",
            (event.event_id,),
        )
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal:
        plans = ReadCompanyFieldRecovery(formal, collected, item.codes, NOW, tmp_path)
        assert [(plan.action, plan.reason) for plan in plans] == [("extract", "local_text_available")]
        with SqliteTransaction(collected):
            collected.run(
                "INSERT INTO company_field_repairs SELECT id,'600001',record_hash,?,'planned_pct',"
                "'reduction_total_capital_ratio:3','attempted' FROM disclosures WHERE source=?",
                (event.event_id, item.source),
            )
        plans = ReadCompanyFieldRecovery(formal, collected, item.codes, NOW, tmp_path)
        assert [(plan.action, plan.reason) for plan in plans] == [("verify", "same_rule_already_attempted")]


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
            "SELECT COUNT(*) FROM company_input_receipt WHERE outcome='field_attempted' AND policy_id LIKE 'field:%'"
        ).require_one()[0] == 1
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("DROP TRIGGER fail_after_formal_commit")

    restarted = fixtures._index(tmp_path)
    restarted.prepare_publication(item.codes, NOW + timedelta(minutes=1))
    assert [tuple(row) for row in _state(restarted)] == [("planned_pct", "complete")]
    assert restarted.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()
    assert not tuple((tmp_path / "text").rglob("*.gz"))  # The same formal commit also published all source proofs.
    with SqliteSession(index.facts_path) as formal:
        assert formal.run("SELECT state FROM company_field_result WHERE field='planned_pct'").require_one()[0] == "verified"


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
        assert formal.run("SELECT COUNT(*) FROM company_input_receipt WHERE outcome='field_attempted' AND policy_id LIKE 'field:%'").require_one()[0] == 0
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


def test_same_body_repurchase_replay_corrects_old_nonnull_purpose_and_missing_actual_ratio(tmp_path):
    index, item = _legacy_repurchase_template(tmp_path)
    with SqliteSession(index.facts_path) as formal:
        before = tuple(formal.run("SELECT * FROM company_input_receipt WHERE outcome='valid'"))
        identity = ReadCompanyPublicationIdentity(formal).extraction_hash

    index.prepare_publication(item.codes, NOW)

    event, = _facts(index).positive_events
    assert event.measurements.actual_ratio_pct == 0.062
    assert event.measurements.purpose is RepurchasePurpose.OTHER
    assert event.status is CompanyPositiveStatus.IMPLEMENTING and event.positive_level == 0
    with SqliteSession(index.facts_path) as formal:
        assert tuple(formal.run("SELECT * FROM company_input_receipt WHERE outcome='valid'")) == before
        assert ReadCompanyPublicationIdentity(formal).extraction_hash == identity
    with SqliteSession(index.path) as collected:
        states = tuple(collected.run(
            "SELECT field,state FROM company_field_repairs WHERE rule_id='same_body_repurchase_fields:1' ORDER BY field"
        ))
        assert [tuple(row) for row in states] == [("actual_pct", "complete"), ("repurchase_purpose", "complete")]
    for _ in range(2):
        index.prepare_publication(item.codes, NOW)
        assert index.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()
        assert _facts(index).positive_events == (event,)


@pytest.mark.parametrize("title", (
    "关于回购股份事项前十大股东持股情况的公告",
    "关于回购股份事项前十名股东持股信息的公告",
    "关于回购股份事项前十名股东和前十名无限售条件股东情况的公告",
    "关于回购股份事项前十名股东和前十名无限售条件股东的公告",
))
def test_holder_table_recheck_retires_only_empty_event_and_review_without_blessing_field_or_redownload(tmp_path, title):
    index, item, _ = _legacy_holder_table(tmp_path, title)
    with SqliteSession(index.facts_path) as formal:
        before = tuple(formal.run("SELECT * FROM company_input_receipt WHERE outcome='valid'"))
        identity = ReadCompanyPublicationIdentity(formal).extraction_hash
    for _ in range(2):
        index.initialize()
        index.prepare_publication(item.codes, NOW)
        assert _facts(index).positive_events == ()
        assert not any(review.evidence_id == item.identity for review in _facts(index).review_reasons)
        assert index.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()
    with SqliteSession(index.facts_path) as formal:
        assert tuple(formal.run("SELECT * FROM company_input_receipt WHERE outcome='valid'")) == before
        assert ReadCompanyPublicationIdentity(formal).extraction_hash == identity
    with SqliteSession(index.path) as collected:
        assert collected.run("SELECT state FROM company_field_repairs WHERE field='repurchase_purpose'").require_one()[0] == "attempted"
        assert collected.run("SELECT 1 FROM company_document_requirements").fetchone() is None


def test_repurchase_recheck_does_not_complete_from_old_nonnull_value_before_new_receipt(tmp_path):
    index, item = _legacy_repurchase_template(tmp_path)
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal, SqliteTransaction(collected):
        identity = ReadCompanyPublicationIdentity(formal).extraction_hash
        QueueCompanyRepurchaseRechecks(
            collected, formal, item.codes, NOW, identity, fixtures.CompanyEventPolicy(),
            CompanyDocumentFilesFixture(tmp_path / "text").read,
        )
        RestoreCompanyFieldRepairs(collected, formal)
        pending = tuple(collected.run(
            "SELECT state FROM company_field_repairs WHERE rule_id='same_body_repurchase_fields:1'"
        ))
        assert [row[0] for row in pending] == ["pending", "pending"]
        assert collected.run("SELECT 1 FROM document_publications WHERE code=?", (item.codes[0],)).fetchone() is None
    index.prepare_publication(item.codes, NOW)
    assert _facts(index).positive_events[0].measurements.purpose is RepurchasePurpose.OTHER


def test_holder_table_recheck_clears_old_subject_review_without_creating_a_repurchase(tmp_path):
    index, item, _ = _legacy_holder_table(tmp_path)
    with SqliteSession(index.facts_path) as formal:
        parent = ReadCompanyPublicationIdentity(formal)
        before = tuple(formal.run("SELECT * FROM company_input_receipt WHERE outcome='valid'"))
    SQLiteCompanyFactsIndexFixture(index.facts_path).publish(
        (replace(_facts(index), positive_events=(), review_reasons=(
            CompanyReviewReason("positive", "subject_unverified", item.identity, NOW, "旧主体待核"),
        )),), {item.identity: item.source}, parent, NOW,
    )
    index.initialize()
    index.prepare_publication(item.codes, NOW)
    assert _facts(index).positive_events == ()
    assert not any(review.evidence_id == item.identity for review in _facts(index).review_reasons)
    with SqliteSession(index.facts_path) as formal:
        assert tuple(formal.run("SELECT * FROM company_input_receipt WHERE outcome='valid'")) == before


def test_repurchase_recheck_does_not_request_missing_local_material(tmp_path):
    index, item = _legacy_repurchase_template(tmp_path)
    def unavailable(_digest):
        raise ValueError("fixture_missing_text")

    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal, SqliteTransaction(collected):
        QueueCompanyRepurchaseRechecks(
            collected, formal, item.codes, NOW, ReadCompanyPublicationIdentity(formal).extraction_hash,
            fixtures.CompanyEventPolicy(), unavailable,
        )
        assert collected.run(
            "SELECT 1 FROM company_field_repairs WHERE rule_id='same_body_repurchase_fields:1'"
        ).fetchone() is None
    event, = _facts(index).positive_events
    assert event.measurements.purpose is RepurchasePurpose.EMPLOYEE_PLAN
    assert event.measurements.actual_ratio_pct is None


@pytest.mark.parametrize("guard", ("missing_text", "missing_receipt", "known_purpose", "linked_progress"))
def test_holder_table_recheck_keeps_unproven_or_measured_previous_events(tmp_path, guard):
    index, item, legacy = _legacy_holder_table(tmp_path)
    if guard == "missing_receipt":
        with SqliteSession(index.facts_path) as formal, SqliteTransaction(formal):
            formal.run("DELETE FROM company_input_receipt WHERE outcome='valid'")
        with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal, SqliteTransaction(collected):
            QueueCompanyRepurchaseRechecks(
                collected, formal, item.codes, NOW, ReadCompanyPublicationIdentity(formal).extraction_hash,
                fixtures.CompanyEventPolicy(), CompanyDocumentFilesFixture(tmp_path / "text").read,
            )
            assert collected.run("SELECT 1 FROM document_publications WHERE code=?", (item.codes[0],)).fetchone() is not None
        with SqliteSession(index.facts_path) as formal:
            assert [row[0] for row in formal.run("SELECT event_key FROM company_positive_event")] == [legacy.event_id]
        return
    elif guard == "missing_text":
        with SqliteSession(index.path) as collected, SqliteTransaction(collected):
            collected.run("UPDATE disclosures SET body_retained=0 WHERE source_key=?", (item.identity,))
    else:
        old = replace(legacy, measurements=replace(legacy.measurements, purpose=RepurchasePurpose.EMPLOYEE_PLAN))
        if guard == "linked_progress":
            old = replace(legacy, evidence_ids=(item.identity, "actual-progress"))
        with SqliteSession(index.facts_path) as formal:
            parent = ReadCompanyPublicationIdentity(formal)
        SQLiteCompanyFactsIndexFixture(index.facts_path).publish(
            (replace(_facts(index), positive_events=(old,)),), {item.identity: item.source, "actual-progress": item.source}, parent, NOW,
        )
    index.initialize()
    index.prepare_publication(item.codes, NOW)
    assert len(_facts(index).positive_events) == 1
    assert index.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()
    if guard == "known_purpose":
        with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal:
            links, = ReadCompanyPublicationLinks(
                collected, item.codes, ReadCompanyPublicationIdentity(formal).extraction_hash,
                observed_at=NOW, event_policy=fixtures.CompanyEventPolicy(),
                read_document=CompanyDocumentFilesFixture(tmp_path / "text").read,
            )
            assert links.candidates == ()


def test_repurchase_recheck_keeps_same_source_multiple_events_unmodified(tmp_path):
    index, item = _legacy_repurchase_template(tmp_path)
    facts = _facts(index)
    first, = facts.positive_events
    second = replace(first, event_id="another-repurchase")
    formal_index = SQLiteCompanyFactsIndexFixture(index.facts_path)
    with SqliteSession(index.facts_path) as formal:
        parent = ReadCompanyPublicationIdentity(formal)
    formal_index.publish((replace(facts, positive_events=(first, second)),), {item.identity: item.source}, parent, NOW)
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal, SqliteTransaction(collected):
        QueueCompanyRepurchaseRechecks(
            collected, formal, item.codes, NOW, ReadCompanyPublicationIdentity(formal).extraction_hash,
            fixtures.CompanyEventPolicy(), CompanyDocumentFilesFixture(tmp_path / "text").read,
        )
        assert collected.run(
            "SELECT 1 FROM company_field_repairs WHERE rule_id='same_body_repurchase_fields:1'"
        ).fetchone() is None


def test_unlinked_repurchase_cache_uses_fixed_measurements_when_plan_arrives(tmp_path):
    index = fixtures._index(tmp_path)
    progress = fixtures._disclosure("progress", "关于回购股份进展的公告")
    body = ("本公司根据公告plan，首次回购股份100万股，占公司总股本的0.062%。"
            "回购用途□减少注册资本□用于员工持股计划或股权激励√用于转换公司可转债。" + _DETAIL)
    _put(index, progress, body)
    index.publish(progress.codes, NOW)
    assert not _facts(index).positive_events
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        row = collected.run("SELECT payload FROM document_link_candidates WHERE source_key='progress'").require_one()
        candidate = DecodeCompanyLink(row[0])
        old = replace(candidate.positive, status=CompanyPositiveStatus.PLANNED, positive_level=0, level_milestones=(),
                      measurements=replace(candidate.positive.measurements, actual_ratio_pct=None, purpose=RepurchasePurpose.EMPLOYEE_PLAN))
        collected.run("UPDATE document_link_candidates SET payload=? WHERE source_key='progress'",
                      (EncodeCompanyLink(replace(candidate, positive=old)),))
    with SqliteSession(index.facts_path) as formal:
        receipt = tuple(formal.run("SELECT * FROM company_input_receipt WHERE source_key='progress' AND outcome='valid'"))

    index.prepare_publication(progress.codes, NOW)
    with SqliteSession(index.path) as collected:
        refreshed = DecodeCompanyLink(collected.run(
            "SELECT payload FROM document_link_candidates WHERE source_key='progress'"
        ).require_one()[0])
        assert refreshed.positive.measurements.actual_ratio_pct == 0.062
        assert refreshed.positive.measurements.purpose is RepurchasePurpose.OTHER
    assert not _facts(index).positive_events
    plan = fixtures._disclosure("plan", "关于回购股份的公告", when=NOW - timedelta(days=1))
    _put(index, plan, "本公司拟回购股份，股份用于转换公司可转债。" + _DETAIL)
    index.publish(plan.codes, NOW)
    event, = _facts(index).positive_events
    assert event.measurements.actual_ratio_pct == 0.062
    assert event.measurements.purpose is RepurchasePurpose.OTHER and event.positive_level == 0
    with SqliteSession(index.facts_path) as formal:
        assert tuple(formal.run("SELECT * FROM company_input_receipt WHERE source_key='progress' AND outcome='valid'")) == receipt


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


@pytest.mark.parametrize(("title", "statement"), (
    ("关于首次回购公司股份的公告", "本公司首次回购股份100万股，拟回购股份占公司总股本的比例为0.3%。回购股份用于员工持股计划。"),
    ("控股股东增持股份公告", "本公司控股股东已增持股份100万股，实际比例详见附表。"),
))
@pytest.mark.parametrize("interrupted", (False, True))
def test_same_body_planned_status_refreshes_formal_applicability_once_without_reissuing_valid(
    tmp_path, title, statement, interrupted,
):
    index, item, writer = _same_body_planned_actual(tmp_path, title, statement)
    reader = SQLiteCompanyEvidenceReaderFixture(index.facts_path)
    old_snapshot = reader.read_fields(item.codes)
    old_actual, = (value for value in old_snapshot.results if value.field == "actual_pct")
    assert old_actual.state == "not_applicable" and not old_actual.applicable
    with SqliteSession(index.facts_path) as formal:
        valid = tuple(tuple(row) for row in formal.run("SELECT * FROM company_input_receipt WHERE outcome='valid'"))

    if interrupted:
        with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal, SqliteTransaction(collected):
            QueueCompanyRepurchaseRechecks(
                collected, formal, item.codes, NOW, writer.extraction_hash,
                fixtures.CompanyEventPolicy(), CompanyDocumentFilesFixture(tmp_path / "text").read,
            )
            assert collected.run("SELECT 1 FROM document_publications WHERE code=?", (item.codes[0],)).fetchone() is None
        assert _facts(index).positive_events[0].status is CompanyPositiveStatus.PLANNED
        index = fixtures._index(tmp_path)
    index.prepare_publication(item.codes, NOW)

    event, = _facts(index).positive_events
    actual, = (value for value in reader.read_fields(item.codes).results if value.field == "actual_pct")
    assert event.status is CompanyPositiveStatus.IMPLEMENTING and event.measurements.actual_ratio_pct is None
    assert event.positive_level == 0 and event.level_milestones == ()
    assert actual.applicable and actual.state == "verification_required" and actual.value is None and actual.evidence == ()
    assert reader.publication_version() != old_snapshot.publication_hash
    with SqliteSession(index.facts_path) as formal:
        attempts = tuple(tuple(row) for row in formal.run(
            "SELECT * FROM company_input_receipt WHERE outcome='field_attempted' ORDER BY policy_id"
        ))
    assert any(row[-1].endswith(":actual_pct:implemented_total_capital_ratio:2") for row in attempts)
    for minute in (1, 2):
        restarted = fixtures._index(tmp_path)
        restarted.prepare_publication(item.codes, NOW + timedelta(minutes=minute))
        assert restarted.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()
        with SqliteSession(index.facts_path) as formal:
            assert tuple(tuple(row) for row in formal.run("SELECT * FROM company_input_receipt WHERE outcome='valid'")) == valid
            assert tuple(tuple(row) for row in formal.run(
                "SELECT * FROM company_input_receipt WHERE outcome='field_attempted' ORDER BY policy_id"
            )) == attempts
            assert ReadCompanyPublicationIdentity(formal).extraction_hash == writer.extraction_hash


def test_status_only_holder_cache_refresh_keeps_an_already_current_formal_confirmation(tmp_path):
    index, item, writer = _same_body_planned_actual(
        tmp_path, "控股股东增持股份公告", "本公司控股股东已增持股份100万股，实际比例详见附表。",
    )
    facts = _facts(index)
    event, = facts.positive_events
    with SqliteSession(index.facts_path) as formal:
        parent = ReadCompanyPublicationIdentity(formal)
    writer.publish((replace(facts, positive_events=(replace(event, status=CompanyPositiveStatus.IMPLEMENTING),)),),
                   {item.identity: item.source}, parent, NOW)
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal, SqliteTransaction(collected):
        before = ReadCompanyPublicationIdentity(formal)
        QueueCompanyRepurchaseRechecks(
            collected, formal, item.codes, NOW, writer.extraction_hash,
            fixtures.CompanyEventPolicy(), CompanyDocumentFilesFixture(tmp_path / "text").read,
        )
        current = DecodeCompanyLink(collected.run(
            "SELECT payload FROM document_link_candidates WHERE source_key=?", (item.identity,),
        ).require_one()[0])
        assert current.positive.status is CompanyPositiveStatus.IMPLEMENTING
        assert collected.run("SELECT 1 FROM document_publications WHERE code=?", (item.codes[0],)).fetchone() is not None
        assert ReadCompanyPublicationIdentity(formal) == before


@pytest.mark.parametrize("guard", ("source_version", "missing_receipt", "different_subject", "multiple_events", "missing_text"))
def test_status_only_holder_refresh_requires_exact_material_and_one_matching_event(tmp_path, guard):
    index, item, writer = _same_body_planned_actual(
        tmp_path, "控股股东增持股份公告", "本公司控股股东已增持股份100万股，实际比例详见附表。",
    )
    if guard == "source_version":
        with SqliteSession(index.path) as collected, SqliteTransaction(collected):
            collected.run("UPDATE disclosures SET record_hash='different-source-version' WHERE source_key=?", (item.identity,))
    elif guard == "missing_receipt":
        with SqliteSession(index.facts_path) as formal, SqliteTransaction(formal):
            formal.run("DELETE FROM company_input_receipt WHERE outcome='valid'")
    elif guard == "different_subject":
        with SqliteSession(index.path) as collected, SqliteTransaction(collected):
            cached = DecodeCompanyLink(collected.run(
                "SELECT payload FROM document_link_candidates WHERE source_key=?", (item.identity,),
            ).require_one()[0])
            collected.run("UPDATE document_link_candidates SET payload=? WHERE source_key=?",
                          (EncodeCompanyLink(replace(cached, subject=CompanySubject.ACTUAL_CONTROLLER)), item.identity))
    elif guard == "multiple_events":
        facts = _facts(index)
        event, = facts.positive_events
        with SqliteSession(index.facts_path) as formal:
            parent = ReadCompanyPublicationIdentity(formal)
        writer.publish((replace(facts, positive_events=(event, replace(event, event_id="independent-action"))),),
                       {item.identity: item.source}, parent, NOW)
    else:
        for path in (tmp_path / "text").rglob("*.gz"):
            path.unlink()
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal:
        before = collected.run("SELECT payload FROM document_link_candidates WHERE source_key=?", (item.identity,)).require_one()[0]
        valid = tuple(tuple(row) for row in formal.run("SELECT * FROM company_input_receipt WHERE outcome='valid'"))
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal, SqliteTransaction(collected):
        QueueCompanyRepurchaseRechecks(
            collected, formal, item.codes, NOW, writer.extraction_hash,
            fixtures.CompanyEventPolicy(), CompanyDocumentFilesFixture(tmp_path / "text").read,
        )
        assert collected.run("SELECT payload FROM document_link_candidates WHERE source_key=?", (item.identity,)).require_one()[0] == before
        assert collected.run("SELECT 1 FROM company_dirty_codes WHERE code=?", (item.codes[0],)).fetchone() is None
        assert collected.run("SELECT 1 FROM document_publications WHERE code=?", (item.codes[0],)).fetchone() is not None
        assert tuple(tuple(row) for row in formal.run("SELECT * FROM company_input_receipt WHERE outcome='valid'")) == valid


@pytest.mark.parametrize("valid_receipt", (True, False))
def test_retained_issuer_plan_recheck_links_progress_without_reissuing_valid_receipt(tmp_path, valid_receipt):
    index = fixtures._index(tmp_path)
    plan = fixtures._disclosure("plan", "关于回购股份的回购报告书", when=NOW - timedelta(days=1))
    progress = fixtures._disclosure("progress", "关于回购股份进展的公告")
    _put(index, plan, "本公司董事会保证公告真实。公司将在回购期限内根据市场情况择机作出回购决策予以实施。"
                    "控股股东暂无减持计划。" + _DETAIL)
    _put(index, progress, "本公司根据公告plan，首次回购股份100万股，占公司总股本的0.062%。"
                        "回购用途□减少注册资本□用于员工持股计划或股权激励√用于转换公司可转债。" + _DETAIL)
    index.publish(plan.codes, NOW)
    with SqliteSession(index.facts_path) as formal:
        parent = ReadCompanyPublicationIdentity(formal)
        before = tuple(formal.run("SELECT * FROM company_input_receipt WHERE outcome='valid'"))
    facts = _facts(index)
    review = CompanyReviewReason("positive", "subject_unverified", "plan", NOW, "旧规则主体不明")
    unlinked = CompanyReviewReason("positive", "progress_unlinked", "progress", NOW, "等待方案")
    SQLiteCompanyFactsIndexFixture(index.facts_path).publish(
        (replace(facts, positive_events=(), review_reasons=(review, unlinked)),),
        {"plan": "cninfo", "progress": "cninfo"}, parent, NOW,
    )
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("DELETE FROM document_link_candidates WHERE source_key='plan'")
    if not valid_receipt:
        with SqliteSession(index.facts_path) as formal, SqliteTransaction(formal):
            formal.run("DELETE FROM company_input_receipt WHERE source_key='plan' AND outcome='valid'")
            before = tuple(formal.run("SELECT * FROM company_input_receipt WHERE outcome='valid'"))

    index.prepare_publication(plan.codes, NOW)

    if valid_receipt:
        event, = _facts(index).positive_events
        assert event.event_id == "plan:share_repurchase"
        assert event.evidence_ids == ("plan", "progress")
        assert event.measurements.actual_ratio_pct == 0.062
        assert event.measurements.purpose is RepurchasePurpose.OTHER and event.positive_level == 0
        for _ in range(2):
            index.prepare_publication(plan.codes, NOW)
            assert index.pending_documents(plan.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()
    else:
        assert _facts(index).positive_events == ()
    with SqliteSession(index.facts_path) as formal:
        assert tuple(formal.run("SELECT * FROM company_input_receipt WHERE outcome='valid'")) == before
        assert ReadCompanyPublicationIdentity(formal).extraction_hash == parent.extraction_hash


@pytest.mark.parametrize("valid_receipt", (True, False))
def test_verified_progress_prioritizes_existing_plan_before_newer_unrelated_documents(tmp_path, valid_receipt):
    index = fixtures._index(tmp_path)
    progress = fixtures._disclosure("progress", "关于回购股份进展的公告")
    _put(index, progress, "本公司根据公告plan，累计回购股份占总股本比例为0.3%，股份用于注销。" + _DETAIL)
    index.publish(progress.codes, NOW)
    plan = fixtures._disclosure("plan", "关于回购股份的回购报告书", when=NOW - timedelta(days=2))
    unrelated = fixtures._disclosure("unrelated", "关于回购股份用途安排的公告",
                                     when=NOW - timedelta(days=1))
    for item in (plan, unrelated):
        index.commit_page(DisclosurePartition("sse", item.published_at.date(), item.published_at.date()),
                          DisclosurePage((item,), 1, False), NOW)
    if not valid_receipt:
        with SqliteSession(index.path) as collected, SqliteTransaction(collected):
            collected.run("UPDATE disclosures SET record_hash='unverified-version' WHERE source_key='progress'")

    tasks = index.pending_documents(plan.codes, NOW, limit=1, policies=fixtures._TEST_DOCUMENT_POLICIES)

    assert tasks[0].disclosure.identity == ("plan" if valid_receipt else "unrelated")
    all_tasks = index.pending_documents(plan.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES)
    assert {task.disclosure.identity for task in all_tasks}.issuperset({"plan", "unrelated"})
    assert not _facts(index).positive_events
