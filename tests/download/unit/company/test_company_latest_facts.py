"""Dated snapshots cannot regress when an older source finishes later."""

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from trader.download.domain.company_events import CompanyDataState
from trader.download.domain.company_event_extraction import CompanyDocumentNeedsReview, CompanyDocumentRiskRelevant
from trader.download.domain.company_evidence import CompanyDocument, CompanyEvidenceInput, CompanyRatio
from trader.download.domain.company_facts import CompileCompanyFacts

from trader.download.domain.company_event_extraction import IsCompanyRepurchaseShareholderTable


@pytest.mark.parametrize("title", (
    "关于董事减持股份计划的公告",
    "关于高级管理人员增持公司股份的公告",
    "关于监事收到警示函的公告",
    "关于子公司签订重大合同的公告",
    "关于参股公司收到行政处罚决定书的公告",
))
def test_existing_subject_exclusions_need_no_body_and_leave_no_pending_risk(title):
    item = fixtures._disclosure(title=title)
    source = CompanyEvidenceInput(
        "600001", fixtures.NOW, fixtures.NOW.date() - timedelta(days=365), fixtures.NOW.date(),
        "test", (CompanyDocument(item, "", False),), True,
        (), None, (), False, (), False, (),
    )
    assert not CompanyDocumentNeedsReview(title)
    assert not CompanyDocumentRiskRelevant(title)
    pending = CompileCompanyFacts(source, fixtures.CompanyEventPolicy())
    extracted = CompileCompanyFacts(
        replace(source, announcements=(CompanyDocument(item, "本公司已披露该事项，详情见正式公告。", True),)),
        fixtures.CompanyEventPolicy(),
    )
    assert pending == extracted
    assert pending.pending_document_count == 0
    assert not pending.risk_events and not pending.positive_events
    assert not pending.review_reasons


@pytest.mark.parametrize("title", (
    "关于董事会审议回购股份方案的公告",
    "监事会关于资金占用事项的说明",
    "关于控股股东兼董事减持计划的公告",
    "关于实际控制人兼董事增持股份的公告",
    "关于持股5%以上股东兼董事减持股份的公告",
    "关于5%以上股东兼董事减持计划的公告",
    "关于大股东兼董事减持计划的公告",
    "关于公司及子公司收到警示函的公告",
    "关于本公司签订重大合同的公告",
))
def test_subject_titles_that_may_concern_issuer_or_qualifying_holder_remain_required(title):
    assert CompanyDocumentNeedsReview(title)


def test_late_older_pledge_keeps_newer_value_and_reports_incomplete_source():
    now = fixtures.NOW
    latest = CompanyRatio("600001", now.date(), 2.0)
    source = CompanyEvidenceInput(
        "600001", now, now.date() - timedelta(days=365), now.date(), "test", (), True,
        (), latest, (), False, (), False, (),
    )
    previous = CompileCompanyFacts(source, fixtures.CompanyEventPolicy())
    older = CompanyRatio("600001", now.date() - timedelta(days=1), 8.0)
    current = CompileCompanyFacts(replace(source, pledge=older), fixtures.CompanyEventPolicy(), previous=previous)
    assert current.pledge == latest
    assert current.pledge_state is CompanyDataState.PARTIAL


def test_future_pledge_cannot_replace_latest_effective_snapshot():
    now = fixtures.NOW
    latest = CompanyRatio("600001", now.date(), 2.0)
    source = CompanyEvidenceInput(
        "600001", now, now.date() - timedelta(days=365), now.date(), "test", (), True,
        (), latest, (), False, (), False, (),
    )
    previous = CompileCompanyFacts(source, fixtures.CompanyEventPolicy())
    future = CompanyRatio("600001", now.date() + timedelta(days=1), 0.0)
    current = CompileCompanyFacts(replace(source, pledge=future), fixtures.CompanyEventPolicy(), previous=previous)
    assert current.pledge == latest
    assert current.pledge_state is CompanyDataState.PARTIAL


def _unlock_source() -> CompanyEvidenceInput:
    now = fixtures.NOW
    return CompanyEvidenceInput(
        code="600001",
        published_at=now,
        window_start=now.date() - timedelta(days=365),
        window_end=now.date(),
        version="test",
        announcements=(),
        announcement_complete=True,
        financials=(),
        pledge=None,
        unlocks=(
            CompanyRatio("600001", now.date() + timedelta(days=1), 2.0),
            CompanyRatio("600001", now.date() + timedelta(days=30), 3.0),
        ),
        unlock_checked=True,
        news=(),
        news_checked=False,
        failures=(),
        unlock_checked_at=now,
        unlock_coverage_end=now.date() + timedelta(days=180),
    )


def test_unlock_failure_preserves_previous_snapshot_dates_and_only_pending_schedule():
    source = _unlock_source()
    previous = CompileCompanyFacts(source, fixtures.CompanyEventPolicy())
    later = source.published_at + timedelta(days=2)
    failed = replace(
        source,
        published_at=later,
        window_end=later.date(),
        unlocks=(),
        unlock_checked=False,
        unlock_checked_at=None,
        unlock_coverage_end=None,
        failures=("unlock:timeout",),
    )
    current = CompileCompanyFacts(failed, fixtures.CompanyEventPolicy(), previous=previous)
    assert current.unlock_schedule == source.unlocks[1:]
    assert current.unlock_checked_at == previous.unlock_checked_at
    assert current.unlock_coverage_end == previous.unlock_coverage_end
    assert current.unlock_state is CompanyDataState.PARTIAL


def test_complete_empty_unlock_snapshot_retracts_previous_schedule():
    source = _unlock_source()
    previous = CompileCompanyFacts(source, fixtures.CompanyEventPolicy())
    later = source.published_at + timedelta(days=1)
    empty = replace(
        source,
        published_at=later,
        window_end=later.date(),
        unlocks=(),
        unlock_checked_at=later,
        unlock_coverage_end=later.date() + timedelta(days=180),
    )
    current = CompileCompanyFacts(empty, fixtures.CompanyEventPolicy(), previous=previous)
    assert current.unlock_schedule == ()
    assert current.unlock_checked_at == later and current.unlock_coverage_end == empty.unlock_coverage_end
    assert current.unlock_state is CompanyDataState.COMPLETE


def test_failed_refresh_keeps_a_newer_verified_empty_unlock_snapshot():
    source = _unlock_source()
    previous = CompileCompanyFacts(source, fixtures.CompanyEventPolicy())
    later = source.published_at + timedelta(days=1)
    cached = replace(
        source,
        published_at=later,
        window_end=later.date(),
        unlocks=(),
        unlock_checked=False,
        unlock_checked_at=later,
        unlock_coverage_end=later.date() + timedelta(days=180),
        failures=("unlock:timeout",),
    )
    current = CompileCompanyFacts(cached, fixtures.CompanyEventPolicy(), previous=previous)
    assert current.unlock_schedule == () and current.unlock_state is CompanyDataState.PARTIAL
    assert current.unlock_checked_at == cached.unlock_checked_at
    assert current.unlock_coverage_end == cached.unlock_coverage_end


def test_explicit_complete_unlock_check_preserves_its_recorded_timestamp():
    source = _unlock_source()
    stale = replace(source, unlock_checked_at=source.published_at - timedelta(days=2))
    current = CompileCompanyFacts(stale, fixtures.CompanyEventPolicy())
    assert current.unlock_schedule == source.unlocks
    assert current.unlock_checked_at == stale.unlock_checked_at
    assert current.unlock_state is CompanyDataState.COMPLETE


@pytest.mark.parametrize("kind", ("older", "future", "naive", "short_coverage", "expired_coverage"))
def test_unusable_or_short_unlock_snapshot_cannot_erase_previous_schedule(kind):
    source = _unlock_source()
    previous = CompileCompanyFacts(source, fixtures.CompanyEventPolicy())
    checked_at, coverage_end = source.unlock_checked_at, source.unlock_coverage_end
    if kind == "older":
        checked_at -= timedelta(days=1)
    elif kind == "future":
        checked_at += timedelta(days=1)
    elif kind == "naive":
        checked_at = checked_at.replace(tzinfo=None)
    elif kind == "short_coverage":
        coverage_end -= timedelta(days=1)
    else:
        checked_at -= timedelta(days=200)
        coverage_end = source.published_at.date() - timedelta(days=1)
    current = CompileCompanyFacts(
        replace(source, unlocks=(), unlock_checked_at=checked_at, unlock_coverage_end=coverage_end),
        fixtures.CompanyEventPolicy(), previous=previous,
    )
    assert current.unlock_schedule == previous.unlock_schedule
    assert current.unlock_checked_at == previous.unlock_checked_at
    assert current.unlock_coverage_end == previous.unlock_coverage_end
    assert current.unlock_state is CompanyDataState.PARTIAL


def test_expired_previous_unlock_coverage_does_not_become_a_current_empty_snapshot():
    source = _unlock_source()
    previous = CompileCompanyFacts(source, fixtures.CompanyEventPolicy())
    later = source.published_at + timedelta(days=181)
    current = CompileCompanyFacts(
        replace(
            source,
            published_at=later,
            window_end=later.date(),
            unlocks=(),
            unlock_checked=False,
            unlock_checked_at=None,
            unlock_coverage_end=None,
            failures=("unlock:timeout",),
        ),
        fixtures.CompanyEventPolicy(), previous=previous,
    )
    assert current.unlock_schedule == () and current.unlock_state is CompanyDataState.UNAVAILABLE
    assert current.unlock_checked_at == previous.unlock_checked_at
    assert current.unlock_coverage_end == previous.unlock_coverage_end


def test_future_previous_unlock_snapshot_is_not_reused_after_source_failure():
    source = _unlock_source()
    previous = replace(
        CompileCompanyFacts(source, fixtures.CompanyEventPolicy()),
        unlock_checked_at=source.published_at + timedelta(days=1),
    )
    current = CompileCompanyFacts(
        replace(
            source,
            unlocks=(),
            unlock_checked=False,
            unlock_checked_at=None,
            unlock_coverage_end=None,
            failures=("unlock:timeout",),
        ),
        fixtures.CompanyEventPolicy(),
        previous=previous,
    )
    assert current.unlock_schedule == () and current.unlock_state is CompanyDataState.UNAVAILABLE
    assert current.unlock_checked_at is None and current.unlock_coverage_end is None


@pytest.mark.parametrize("title", (
    "关于前十名股东表决股份回购事项的公告",
    "关于回购股份事项前十名股东表态的公告",
    "关于回购股份事项前十名股东的公告暨回购股份资金用途的说明",
))
def test_holder_table_words_do_not_exclude_other_event_material(title):
    assert not IsCompanyRepurchaseShareholderTable(title)
    assert CompanyDocumentNeedsReview(title)


@pytest.mark.parametrize("title", (
    "关于回购股份事项前十大股东持股情况的公告",
    "关于回购公司股份的前十名股东和前十名无限售条件股东持股情况的公告",
    "关于股份回购事项前10名普通股股东持股情况的公告",
    "关于回购股份事项前十名股东持股信息的公告",
    "关于回购股份事项前十名股东和前十名无限售条件股东情况的公告",
    "上海艾力斯医药科技股份有限公司关于回购股份事项前十名股东和前十名无限售条件股东的公告",
))
def test_standalone_repurchase_holder_tables_are_not_event_inputs(title):
    assert IsCompanyRepurchaseShareholderTable(title)
    assert not CompanyDocumentNeedsReview(title)
    assert not CompanyDocumentRiskRelevant(title)
    item = fixtures._disclosure(title=title)
    source = CompanyEvidenceInput(
        "600001", fixtures.NOW, fixtures.NOW.date() - timedelta(days=365), fixtures.NOW.date(),
        "test", (CompanyDocument(item, "", False),), True,
        (), None, (), False, (), False, (),
    )
    facts = CompileCompanyFacts(source, fixtures.CompanyEventPolicy())
    assert not facts.positive_events and not facts.review_reasons and facts.pending_document_count == 0


@pytest.mark.parametrize("suffix", (
    "暨首次回购进展", "及减持计划", "暨控股股东增持", "及收到警示函", "暨签订重大合同",
    "及实施结果", "及回购方案变更", "及回购报告书", "及风险提示涉及违规担保", "和回购方案", "与回购方案", "、回购方案",
))
def test_holder_table_titles_with_separate_events_remain_required(suffix):
    title = "关于回购股份事项前十大股东持股情况" + suffix + "的公告"
    assert not IsCompanyRepurchaseShareholderTable(title)
    assert CompanyDocumentNeedsReview(title)
