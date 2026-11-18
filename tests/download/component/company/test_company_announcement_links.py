"""Number aliases recover latest event fields without downloading accepted text again."""

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company_resources import SQLiteCompanyEvidenceReaderFixture
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


@pytest.mark.parametrize("label", ("证券代码（A/H）", "证券代码(A股/H股)", "证券代码（A股）", "股票代码"))
def test_explicit_issuer_header_allows_share_class_labels_but_never_later_cited_codes(label):
    numbers = ExtractCompanyAnnouncementNumbers(label + "：000063/00763\n公告编号：2026-101\n本公司说明如下。\n证券代码：600001")
    assert numbers.issuer_codes == ("000063",)


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
