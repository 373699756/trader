"""Number aliases recover latest event fields without downloading accepted text again."""

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.component import test_company_evidence as fixtures
from tests.helpers.configuration import SQLiteCompanyEvidenceReaderFixture
from trader.download.domain.company_announcement_numbers import ExtractCompanyAnnouncementNumbers
from trader.download.domain.company_evidence import DisclosurePage, DisclosurePartition
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction

_DETAIL = "本公司将依照有关规定持续履行信息披露义务，相关内容请以本次正式公告为准。"
_OLD = "证券代码：600001 证券简称：样本 公告编号：2026-101\n关于回购股份的公告\n本公司拟回购股份，回购股份用于注销。" + _DETAIL
_LATEST = "证券代码：600001 证券简称：样本 公告编号：2026-102\n关于回购股份进展的公告\n本公司就公告编号：2026-101所述方案披露实施进展。已实际回购股份占公司总股本的比例为0.2%，回购股份用于注销。" + _DETAIL


@pytest.mark.parametrize("header", ("公告编号：2026-101", "证券代码：600001 公告编号：2026－101"))
def test_own_number_comes_from_header_and_references_remain_separate(header):
    numbers = ExtractCompanyAnnouncementNumbers(
        header + "\n本公司就公告编号：（2025—009）披露进展，另见公告编号：2025-009。"
    )
    assert numbers.own == "2026-101" and numbers.references == ("2025-009",)


def test_inline_citation_cannot_become_own_number_or_accept_partial_numeric_match():
    numbers = ExtractCompanyAnnouncementNumbers(
        "关于回购股份进展的公告\n本公司就公告编号：2025-009披露进展。公告编号：2025-12345不是支持的编号。"
    )
    assert numbers.own is None and numbers.references == ("2025-009",)


def test_conflicting_header_numbers_and_unbounded_reference_lists_remain_unlinked():
    assert ExtractCompanyAnnouncementNumbers("公告编号：2026-001\n公告编号：2026-002").references == ()
    numbers = ExtractCompanyAnnouncementNumbers(
        "公告编号：2026-101\n本公司说明：" + "；".join(f"公告编号：2025-{n:03}" for n in range(33))
    )
    assert numbers.own == "2026-101" and numbers.references == ()


def _put(index, item, body, observed_at):
    index.commit_page(
        DisclosurePartition("sse", item.published_at.date(), item.published_at.date()),
        DisclosurePage((item,), 1, False), observed_at,
    )
    assert index.commit_document(item, fixtures._extraction(body), observed_at)


def _read(index):
    return SQLiteCompanyEvidenceReaderFixture(index.facts_path).read(("600001",))[0]


def test_latest_progress_links_after_older_plan_arrives_and_survives_three_restarts(tmp_path):
    index = fixtures._index(tmp_path)
    observed = fixtures.NOW + timedelta(days=3)
    old = fixtures._disclosure("1225000001", "关于回购股份的公告")
    latest = fixtures._disclosure("1225000002", "关于回购股份进展的公告", when=fixtures.NOW + timedelta(days=2))
    _put(index, latest, _LATEST, observed)
    index.publish(latest.codes, observed)
    assert not _read(index).positive_events
    assert any(reason.reason_code == "progress_unlinked" for reason in _read(index).review_reasons)
    _put(index, old, _OLD, observed)
    index.publish(old.codes, observed)
    facts = _read(index)
    assert len(facts.positive_events) == 1
    event = facts.positive_events[0]
    assert event.evidence_ids == (old.identity, latest.identity)
    assert event.first_disclosed_at == old.published_at
    assert event.last_disclosed_at == latest.published_at
    assert event.measurements.actual_ratio_pct == 0.2
    assert not any(reason.reason_code == "progress_unlinked" for reason in facts.review_reasons)
    with SqliteSession(index.path) as connection:
        receipts = connection.run("SELECT count(*) FROM document_publications").require_one()[0]
    for _ in range(3):
        index.initialize()
        index.prepare_publication(old.codes, observed)
        index.publish(old.codes, observed)
        assert not index.pending_documents(old.codes, observed, policies=fixtures._TEST_DOCUMENT_POLICIES)
        assert _read(index).positive_events == facts.positive_events
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT count(*) FROM document_publications").require_one()[0] == receipts


def test_number_linked_latest_field_closes_old_repair_and_keeps_original_receipt(tmp_path):
    index = fixtures._index(tmp_path)
    observed = fixtures.NOW + timedelta(days=3)
    old = fixtures._disclosure("1225000001", "关于回购股份的公告")
    latest = fixtures._disclosure("1225000002", "关于回购股份进展的公告", when=fixtures.NOW + timedelta(days=2))
    _put(index, old, _OLD.replace("，回购股份用于注销。", "。"), observed)
    index.publish(old.codes, observed)
    with SqliteSession(index.path) as connection:
        original_receipt = tuple(connection.run("SELECT * FROM document_publications").require_one())
        assert connection.run("SELECT state FROM company_field_repairs WHERE field='repurchase_purpose'").require_one()[0] == "attempted"
        assert connection.run("SELECT body_retained FROM disclosures WHERE source_key=?", (old.identity,)).require_one()[0] == 1
    # Existing accepted text is indexed during its normal hash-checked startup
    # recovery. Missing alias metadata must not trigger another download.
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("DELETE FROM document_announcement_numbers")
    index.initialize()
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT number FROM document_announcement_numbers WHERE role=0").require_one()[0] == "2026-101"
    _put(index, latest, _LATEST, observed)
    index.publish(old.codes, observed)
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT state FROM company_field_repairs WHERE field='repurchase_purpose'").require_one()[0] == "complete"
        assert connection.run("SELECT body_retained FROM disclosures WHERE source_key=?", (old.identity,)).require_one()[0] == 0
        assert tuple(connection.run(
            "SELECT * FROM document_publications WHERE disclosure_id=?", (original_receipt[0],)
        ).require_one()) == original_receipt
    events = _read(index).positive_events
    assert events[0].measurements.actual_ratio_pct == 0.2
    for at in (observed, observed + timedelta(days=1)):
        index.prepare_publication(old.codes, at)
        index.publish(old.codes, at, changed_only=True)
        assert not index.pending_documents(old.codes, at, policies=fixtures._TEST_DOCUMENT_POLICIES)
        assert _read(index).positive_events == events
    with SqliteSession(index.path) as connection:
        assert tuple(connection.run(
            "SELECT * FROM document_publications WHERE disclosure_id=?", (original_receipt[0],)
        ).require_one()) == original_receipt


@pytest.mark.parametrize("guard", ("other_stock", "duplicate_number", "same_time", "future", "changed_version", "unknown_reference", "negated_reference", "other_source"))
def test_ambiguous_or_unverified_number_never_links_latest_progress(tmp_path, guard):
    index = fixtures._index(tmp_path)
    observed = fixtures.NOW + timedelta(days=3)
    old = fixtures._disclosure("1225000001", "关于回购股份的公告")
    latest = fixtures._disclosure("1225000002", "关于回购股份进展的公告", when=fixtures.NOW + timedelta(days=2))
    if guard == "other_stock":
        old = replace(old, codes=("600002",))
    elif guard == "other_source":
        old = replace(old, source="issuer_disclosure")
    elif guard == "same_time":
        old = replace(old, published_at=latest.published_at)
    elif guard == "future":
        old = replace(old, published_at=observed)
    _put(index, old, _OLD, observed)
    index.publish(old.codes, observed)
    if guard == "duplicate_number":
        duplicate = replace(old, identity="1225000003", published_at=old.published_at + timedelta(days=1))
        _put(index, duplicate, _OLD, observed)
        index.publish(duplicate.codes, observed)
    if guard == "changed_version":
        with SqliteSession(index.path) as connection, SqliteTransaction(connection):
            connection.run("UPDATE disclosures SET record_hash='changed' WHERE source_key=?", (old.identity,))
    body = _LATEST + ("另见公告编号：2025-099。" if guard == "unknown_reference" else "")
    if guard == "negated_reference":
        body = _LATEST.replace("就公告编号：2026-101所述方案披露实施进展", "说明本次方案与公告编号：2026-101无关")
    _put(index, latest, body, observed)
    index.publish(latest.codes, observed)
    assert any(reason.reason_code == "progress_unlinked" for reason in _read(index).review_reasons)
    assert all(latest.identity not in event.evidence_ids for event in _read(index).positive_events)
