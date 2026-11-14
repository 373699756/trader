"""Synthetic gap fixtures exercise local recovery and real formal-reader round trips."""

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company.company_field_recovery import NOW, _facts, _legacy_reduction, _put
from tests.helpers.download.company_resources import CompanyDocumentFilesFixture, SQLiteCompanyEvidenceReaderFixture, SQLiteCompanyFactsIndexFixture
from trader.download.domain.company_evidence import DisclosurePage, DisclosurePartition
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.download.infra.company_field_recovery import ReadCompanyFieldRecovery, QueueCompanyShareActionRechecks, QueueCompanyReductionRechecks
from trader.download.infra.company_link_codec import DecodeCompanyLink, EncodeCompanyLink
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
    assert (field.value, field.state) == (ratio, "verified")
    assert len(field.evidence) == 1 and field.evidence[0].source_key == item.identity
    assert _valid_receipts(index) == ordinary
    attempted = _attempt_receipts(index, "planned_pct", "reduction_total_capital_ratio:3")
    assert len(attempted) == 1

    restarted = fixtures._index(tmp_path)
    restarted.prepare_publication(item.codes, NOW + timedelta(minutes=2))
    assert restarted.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()
    assert _attempt_receipts(restarted, "planned_pct", "reduction_total_capital_ratio:3") == attempted
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


@pytest.mark.parametrize(("title", "body"), (
    ("控股股东增持计划的法律意见书", "本公司控股股东计划增持公司股份，拟增持比例不低于公司总股本的1%。"),
    ("关于回购股份方案的公告", "本公司拟回购公司股份，拟回购比例不低于公司总股本的1%。"),
))
def test_unchanged_planned_share_action_records_attempt_and_restarts_without_body_reads(tmp_path, monkeypatch, title, body):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("planned-check", title)
    partition = DisclosurePartition("sse", NOW.date(), NOW.date())
    index.commit_page(partition, DisclosurePage((item,), 1, False), NOW)
    assert index.commit_document(item, fixtures._extraction(body + _DETAIL), NOW)
    index.publish(item.codes, NOW)
    before = _facts(index)
    assert len(before.positive_events) == 1
    assert before.positive_events[0].measurements.actual_ratio_pct is None
    valid = _valid_receipts(index)
    reads = []
    read = index._documents.read

    def observed(digest, *, check=None):
        reads.append(digest)
        return read(digest, check=check)

    monkeypatch.setattr(index._documents, "read", observed)
    index.prepare_publication(item.codes, NOW)
    index.publish(item.codes, NOW, changed_only=True)
    assert reads
    assert _facts(index) == before
    assert _valid_receipts(index) == valid
    with SqliteSession(index.facts_path) as formal:
        attempted = tuple(tuple(row) for row in formal.run(
            "SELECT * FROM company_input_receipt WHERE outcome='field_attempted' ORDER BY policy_id",
        ))
        publication = ReadCompanyPublicationIdentity(formal)
    assert attempted
    with SqliteSession(index.path) as collected:
        assert collected.run("SELECT 1 FROM company_field_repairs WHERE state='pending'").fetchone() is None

    restarted = fixtures._index(tmp_path)

    def unexpected_read(*args, **kwargs):
        raise AssertionError("unchanged checked share action must reuse its exact formal attempt")

    monkeypatch.setattr(restarted._documents, "read", unexpected_read)
    restarted.prepare_publication(item.codes, NOW)
    restarted.publish(item.codes, NOW, changed_only=True)
    assert _facts(restarted) == before
    assert _valid_receipts(restarted) == valid
    with SqliteSession(restarted.facts_path) as formal:
        assert ReadCompanyPublicationIdentity(formal) == publication
        assert tuple(tuple(row) for row in formal.run(
            "SELECT * FROM company_input_receipt WHERE outcome='field_attempted' ORDER BY policy_id",
        )) == attempted


@pytest.mark.parametrize("failure", ("missing", "hash", "cancelled"))
def test_unverified_share_action_read_does_not_publish_a_negative_attempt(tmp_path, failure):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("planned-check", "控股股东增持计划的法律意见书")
    body = "本公司控股股东计划增持公司股份，拟增持比例不低于公司总股本的1%。" + _DETAIL
    partition = DisclosurePartition("sse", NOW.date(), NOW.date())
    index.commit_page(partition, DisclosurePage((item,), 1, False), NOW)
    assert index.commit_document(item, fixtures._extraction(body), NOW)
    index.publish(item.codes, NOW)

    def failed_read(digest):
        if failure == "missing":
            raise FileNotFoundError("fixture_missing")
        if failure == "cancelled":
            raise InterruptedError("fixture_cancelled")
        return body + "错配"

    with SqliteSession(index.path) as collected, SqliteTransaction(collected), SqliteSession(index.facts_path) as formal:
        if failure == "cancelled":
            with pytest.raises(InterruptedError, match="fixture_cancelled"):
                QueueCompanyShareActionRechecks(
                    collected, formal, item.codes, NOW, index._facts.extraction_hash,
                    fixtures.CompanyEventPolicy(), failed_read,
                )
        else:
            QueueCompanyShareActionRechecks(
                collected, formal, item.codes, NOW, index._facts.extraction_hash,
                fixtures.CompanyEventPolicy(), failed_read,
            )
        assert collected.run("SELECT 1 FROM company_field_repairs WHERE rule_id LIKE 'same_body_%'").fetchone() is None


@pytest.mark.parametrize("guard", ("body", "record", "rule", "payload"))
def test_share_link_attempt_only_reuses_its_exact_material_and_candidate(tmp_path, guard):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("planned-check", "控股股东增持计划的法律意见书")
    partition = DisclosurePartition("sse", NOW.date(), NOW.date())
    index.commit_page(partition, DisclosurePage((item,), 1, False), NOW)
    body = "本公司控股股东计划增持公司股份，拟增持比例不低于公司总股本的1%。" + _DETAIL
    assert index.commit_document(item, fixtures._extraction(body), NOW)
    index.publish(item.codes, NOW)
    index.prepare_publication(item.codes, NOW)
    with SqliteSession(index.facts_path) as formal:
        assert formal.run(
            "SELECT 1 FROM company_input_receipt WHERE policy_id LIKE '%:same_body_share_link:%'",
        ).fetchone() is not None
        if guard == "body":
            formal.run("UPDATE company_input_receipt SET body_hash=? WHERE policy_id LIKE '%:same_body_share_link:%'", (b"x" * 32,))
        elif guard == "record":
            formal.run("UPDATE company_input_receipt SET record_hash='different' WHERE policy_id LIKE '%:same_body_share_link:%'")
        elif guard == "rule":
            formal.run("UPDATE company_input_receipt SET policy_id='different' WHERE policy_id LIKE '%:same_body_share_link:%'")
    if guard == "payload":
        with SqliteSession(index.path) as collected:
            old = DecodeCompanyLink(collected.run("SELECT payload FROM document_link_candidates").require_one()[0])
            changed = replace(old, positive=replace(old.positive, summary="待核实的旧摘要"))
            collected.run("UPDATE document_link_candidates SET payload=?", (EncodeCompanyLink(changed),))
    reads = []
    read = index._documents.read

    def observed(digest):
        reads.append(digest)
        return read(digest)

    with SqliteSession(index.path) as collected, SqliteTransaction(collected), SqliteSession(index.facts_path) as formal:
        QueueCompanyShareActionRechecks(
            collected, formal, item.codes, NOW, index._facts.extraction_hash,
            fixtures.CompanyEventPolicy(), observed,
        )
    assert reads


@pytest.mark.parametrize("committed", (False, True))
def test_share_check_restart_uses_only_committed_attempt_after_publication_failure(tmp_path, monkeypatch, committed):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("planned-check", "控股股东增持计划的法律意见书")
    partition = DisclosurePartition("sse", NOW.date(), NOW.date())
    index.commit_page(partition, DisclosurePage((item,), 1, False), NOW)
    body = "本公司控股股东计划增持公司股份，拟增持比例不低于公司总股本的1%。" + _DETAIL
    assert index.commit_document(item, fixtures._extraction(body), NOW)
    index.publish(item.codes, NOW)
    valid = _valid_receipts(index)
    publish = index._facts.publish

    def fail_publication(*args, **kwargs):
        if committed:
            publish(*args, **kwargs)
        raise RuntimeError("fixture_publication_interrupted")

    monkeypatch.setattr(index._facts, "publish", fail_publication)
    with pytest.raises(RuntimeError, match="fixture_publication_interrupted"):
        index.prepare_publication(item.codes, NOW)
    restarted = fixtures._index(tmp_path)
    reads = []
    read = restarted._documents.read

    def observed(digest, *, check=None):
        reads.append(digest)
        return read(digest, check=check)

    monkeypatch.setattr(restarted._documents, "read", observed)
    restarted.prepare_publication(item.codes, NOW)
    assert bool(reads) is not committed
    assert _valid_receipts(restarted) == valid


def test_interrupted_valued_share_link_waits_for_formal_attempt_before_retiring(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("valued-check", "关于首次回购公司股份的公告")
    _put(index, item, "本公司首次回购股份100万股，占公司总股本的1%。回购股份用于员工持股计划。" + _DETAIL)
    index.publish(item.codes, NOW)
    assert _facts(index).positive_events[0].measurements.actual_ratio_pct is not None
    with SqliteSession(index.path) as collected, SqliteTransaction(collected), SqliteSession(index.facts_path) as formal:
        QueueCompanyShareActionRechecks(
            collected, formal, item.codes, NOW, index._facts.extraction_hash,
            fixtures.CompanyEventPolicy(), index._documents.read,
        )
        assert collected.run(
            "SELECT 1 FROM company_field_repairs WHERE rule_id LIKE 'same_body_share_link:%' AND state='pending'",
        ).fetchone() is not None
    restarted = fixtures._index(tmp_path)
    restarted.prepare_publication(item.codes, NOW)
    with SqliteSession(index.facts_path) as formal:
        assert formal.run(
            "SELECT 1 FROM company_input_receipt WHERE policy_id LIKE '%:same_body_share_link:%' AND outcome='field_attempted'",
        ).fetchone() is not None

    def unexpected_read(*args, **kwargs):
        raise AssertionError("a valued candidate also requires its committed link-check receipt")

    restarted = fixtures._index(tmp_path)
    monkeypatch.setattr(restarted._documents, "read", unexpected_read)
    restarted.prepare_publication(item.codes, NOW)


def _checked_share_notice(tmp_path, kind):
    index = fixtures._index(tmp_path)
    if kind == "reduction":
        title = "控股股东减持计划公告"
        body = "本公司控股股东拟减持股份，减持期间为2026年10月19日至2026年11月19日。"
    else:
        title = "关于以集中竞价交易方式回购公司股份方案的公告暨回购报告书"
        body = "本次回购方案尚需提交审议，控股股东暂无增持计划。回购用途及实施主体以最终公告为准。"
    item = fixtures._disclosure("checked-notice", title)
    _put(index, item, body + _DETAIL)
    index.publish(item.codes, NOW)
    if kind == "subject":
        assert not _facts(index).positive_events
        assert any(review.scope == "positive" and review.reason_code == "subject_unverified"
                   for review in _facts(index).review_reasons)
    else:
        assert _facts(index).risk_events[0].reduction_plan.planned_ratio_pct is None
    return index, item


@pytest.mark.parametrize("kind", ("reduction", "subject"))
def test_unchanged_reduction_or_unknown_subject_preserves_facts_and_does_not_reread(tmp_path, monkeypatch, kind):
    index, item = _checked_share_notice(tmp_path, kind)
    before, valid = _facts(index), _valid_receipts(index)
    index.prepare_publication(item.codes, NOW)
    assert _facts(index) == before
    assert _valid_receipts(index) == valid
    with SqliteSession(index.path) as collected:
        states = {tuple(row) for row in collected.run(
            "SELECT field,state FROM company_field_repairs WHERE rule_id LIKE 'same_body_reduction:%' "
            "OR rule_id LIKE 'same_body_repurchase_subject:%'",
        )}
    expected = {("start_date", "complete"), ("end_date", "complete"), ("planned_pct", "attempted")} if kind == "reduction" else {
        ("repurchase_purpose", "attempted"),
    }
    assert states == expected
    with SqliteSession(index.facts_path) as formal:
        receipts = tuple(tuple(row) for row in formal.run(
            "SELECT * FROM company_input_receipt WHERE outcome='field_attempted' ORDER BY policy_id",
        ))
        assert any(":same_body_reduction:" in row[-1] if kind == "reduction"
                   else ":same_body_repurchase_subject:" in row[-1] for row in receipts)
        publication = ReadCompanyPublicationIdentity(formal)

    def unexpected_read(*args, **kwargs):
        raise AssertionError("a committed unchanged check must not read its material again")

    restarted = fixtures._index(tmp_path)
    monkeypatch.setattr(restarted._documents, "read", unexpected_read)
    restarted.prepare_publication(item.codes, NOW)
    assert _facts(restarted) == before
    with SqliteSession(index.facts_path) as formal:
        assert ReadCompanyPublicationIdentity(formal) == publication
        assert tuple(tuple(row) for row in formal.run(
            "SELECT * FROM company_input_receipt WHERE outcome='field_attempted' ORDER BY policy_id",
        )) == receipts


@pytest.mark.parametrize("kind", ("reduction", "subject"))
@pytest.mark.parametrize("committed", (False, True))
def test_unchanged_share_notice_requires_formal_commit_across_restart(tmp_path, monkeypatch, kind, committed):
    index, item = _checked_share_notice(tmp_path, kind)
    before, valid = _facts(index), _valid_receipts(index)
    publish = index._facts.publish

    def interrupted(*args, **kwargs):
        if committed:
            publish(*args, **kwargs)
        raise RuntimeError("fixture_interrupted")

    monkeypatch.setattr(index._facts, "publish", interrupted)
    with pytest.raises(RuntimeError, match="fixture_interrupted"):
        index.prepare_publication(item.codes, NOW)
    restarted = fixtures._index(tmp_path)
    reads = []
    read = restarted._documents.read

    def observed(digest, *, check=None):
        reads.append(digest)
        return read(digest, check=check)

    monkeypatch.setattr(restarted._documents, "read", observed)
    restarted.prepare_publication(item.codes, NOW)
    assert bool(reads) is not committed
    assert _facts(restarted) == before and _valid_receipts(restarted) == valid


@pytest.mark.parametrize("kind", ("reduction", "subject"))
@pytest.mark.parametrize("failure", ("missing", "hash", "cancelled"))
def test_failed_share_notice_recheck_does_not_record_an_attempt(tmp_path, kind, failure):
    index, item = _checked_share_notice(tmp_path, kind)

    def failed_read(digest):
        if failure == "missing":
            raise FileNotFoundError("fixture_missing")
        if failure == "cancelled":
            raise InterruptedError("fixture_cancelled")
        return "mismatched material"

    queue = QueueCompanyReductionRechecks if kind == "reduction" else QueueCompanyShareActionRechecks
    with SqliteSession(index.path) as collected, SqliteTransaction(collected), SqliteSession(index.facts_path) as formal:
        if failure == "cancelled":
            with pytest.raises(InterruptedError, match="fixture_cancelled"):
                queue(collected, formal, item.codes, NOW, index._facts.extraction_hash, fixtures.CompanyEventPolicy(), failed_read)
        else:
            queue(collected, formal, item.codes, NOW, index._facts.extraction_hash, fixtures.CompanyEventPolicy(), failed_read)
        assert collected.run(
            "SELECT 1 FROM company_field_repairs WHERE rule_id LIKE 'same_body_reduction:%' "
            "OR rule_id LIKE 'same_body_repurchase_subject:%'",
        ).fetchone() is None
