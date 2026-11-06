"""Contract measurements belong to the business actor, not its disclosure guarantor."""

from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.download.configuration import CompanyEventPolicyFixture
from tests.helpers.recommendation.configuration import CompanyScoringPolicyFixture
from trader.download.domain.company_event_extraction import ExtractCompanyEvents, ExtractCompanyLinkCandidate, ResolveCompanyLinks
from trader.download.domain.company_events import (
    CompanyDataState,
    CompanyPositiveStatus,
    CompanyReviewReason,
    CompanySubject,
    ContractMeasurements,
)
from trader.download.domain.company_evidence import CompanyDocument, CompanyFinancial, CompanyRatio, Disclosure
from trader.download.domain.company_facts import CompanyFacts
from trader.recommendation.domain.scoring.company import (
    AssessCompanyAdjustments,
    BuildCompanyScoringContext,
    BuildScoreLedger,
    CompanyFinancialScores,
    CompanyScoringCalendar,
    ScoreLedgerInput,
)

_NOW = datetime(2026, 11, 5, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
_GUARANTEE = "本公司及董事会全体成员保证信息披露的内容真实、准确、完整，没有虚假记载。"
_REVENUE = (
    "2025年度经审计的营业收入为人民币10000万元（不含税）。"
    "2025年年度报告于2026年4月20日披露。"
)


def _document(body: str, title: str = "关于签署日常经营重大合同的公告") -> CompanyDocument:
    return CompanyDocument(Disclosure("cninfo", "contract", ("002174",), title, _NOW, "", "first"), body, True)


@pytest.mark.parametrize("actor", (
    "公司全资子公司甲公司于近日与乙公司签署了《销售合同》",
    "本公司控股子公司甲公司已签订正式合同",
    "甲股份有限公司（以下简称“公司”）全资子公司乙有限公司（以下简称“乙公司”）于近日采购GPU产品",
    "公司参股公司甲公司已签订正式合同",
))
def test_guarantee_cannot_turn_subsidiary_contract_into_issuer_measurement(actor: str) -> None:
    document = _document(_GUARANTEE + "一、合同签署概况" + actor + "，协议总金额为人民币2000万元（不含税）。" + _REVENUE)
    result = ExtractCompanyEvents((document,), _NOW, CompanyEventPolicyFixture())
    assert result.positives == () and result.risks == () and result.reviews == ()
    assert ExtractCompanyLinkCandidate(document, _NOW, CompanyEventPolicyFixture()) is None


@pytest.mark.parametrize("body", (
    "本公司及子公司已签订正式合同，合同总金额为人民币2000万元（不含税）。",
    "本公司已与全资子公司签订正式合同，合同总金额为人民币2000万元（不含税）。",
    "本公司已签订正式合同。子公司已签订另一合同，合同金额为人民币2000万元（不含税）。",
    "子公司已签订正式合同。甲股份有限公司于近日签订另一合同，合同金额为人民币2000万元（不含税）。",
    "本公司已签订正式合同，交易对手为甲公司的全资子公司。合同金额为人民币2000万元（不含税）。",
    "该重大合同涉及子公司，合同金额为人民币2000万元（不含税）。",
    "甲公司并非本公司的子公司，已签订正式合同。合同金额为人民币2000万元（不含税）。",
))
def test_mixed_or_ambiguous_contract_actors_require_review(body: str) -> None:
    document = _document(_GUARANTEE + body + _REVENUE)
    result = ExtractCompanyEvents((document,), _NOW, CompanyEventPolicyFixture())
    assert result.positives == ()
    assert tuple(review.reason_code for review in result.reviews) == ("subject_unverified",)
    assert ExtractCompanyLinkCandidate(document, _NOW, CompanyEventPolicyFixture()) is None


def test_subsidiary_amount_cannot_leak_from_issuer_context() -> None:
    document = _document(
        _GUARANTEE + "本公司已签订正式合同。合同金额为人民币2000万元（不含税）。"
        "子公司日常经营合同金额为人民币9000万元（不含税）。" + _REVENUE
    )
    result = ExtractCompanyEvents((document,), _NOW, CompanyEventPolicyFixture())
    event = result.positives[0]
    assert event.subject_type is CompanySubject.ISSUER
    assert isinstance(event.measurements, ContractMeasurements)
    assert event.measurements.contract_amount_cny == 20_000_000
    # A separate actor reset also prevents silently inheriting its annual base.
    assert event.measurements.reference_annual_revenue_cny is None
    assert event.positive_level == 0 and result.reviews


def test_source_revision_to_subsidiary_suspends_previous_event_without_cancelling_it() -> None:
    document = _document("本公司已签订正式合同。合同金额为人民币2000万元（不含税）。" + _REVENUE)
    previous = ExtractCompanyEvents((document,), _NOW, CompanyEventPolicyFixture())
    revised = replace(
        document,
        disclosure=replace(document.disclosure, digest="changed"),
        text=_GUARANTEE + "公司全资子公司甲公司已签订正式合同。协议总金额为人民币9000万元（不含税）。" + _REVENUE,
    )
    result = ExtractCompanyEvents(
        (revised,), _NOW, CompanyEventPolicyFixture(), previous_positives=previous.positives
    )
    event = result.positives[0]
    assert event.event_id == previous.positives[0].event_id
    assert event.measurements == previous.positives[0].measurements
    assert event.level_milestones == previous.positives[0].level_milestones
    assert event.status is CompanyPositiveStatus.IMPLEMENTING and event.cancelled_at is None
    assert event.positive_level == 0 and event.not_eligible_reason == "revision_pending"
    assert tuple(review.reason_code for review in result.reviews) == ("subject_unverified",)


@pytest.mark.parametrize("amount_label", ("合同金额", "合同总金额", "协议金额", "协议总金额"))
def test_explicit_agreement_amount_uses_existing_currency_tax_and_time_rules(amount_label: str) -> None:
    document = _document("本公司已签订正式合同。" + amount_label + "为人民币2000万元（不含税）。" + _REVENUE)
    result = ExtractCompanyEvents((document,), _NOW, CompanyEventPolicyFixture())
    event = result.positives[0]
    assert isinstance(event.measurements, ContractMeasurements)
    assert event.measurements.contract_amount_cny == 20_000_000
    assert event.measurements.amount_to_revenue_pct == 20
    assert event.positive_level == 3 and result.reviews == ()


@pytest.mark.parametrize("amount", ("协议总金额为人民币2000万元（含税）", "协议总金额为2000万元（不含税）"))
def test_agreement_amount_without_comparable_explicit_currency_and_tax_cannot_score(amount: str) -> None:
    document = _document("本公司已签订正式合同。" + amount + "。" + _REVENUE)
    result = ExtractCompanyEvents((document,), _NOW, CompanyEventPolicyFixture())
    assert result.positives[0].positive_level == 0 and result.reviews


@pytest.mark.parametrize("holder", ("控股股东", "控股股东兼董事", "实际控制人兼董事", "持股5%以上股东兼董事"))
def test_qualifying_shareholder_increase_is_not_reclassified_by_subsidiary_context(holder: str) -> None:
    document = _document(
        _GUARANTEE + holder + "累计已增持股份，占公司总股本的比例为0.3%。子公司经营正常。",
        "关于" + holder + "增持公司股份的公告",
    )
    result = ExtractCompanyEvents((document,), _NOW, CompanyEventPolicyFixture())
    assert result.positives[0].subject_type is not CompanySubject.ISSUER
    assert result.positives[0].positive_level == 2


@pytest.mark.parametrize("reason,title,scope", (
    ("subject_unverified", "关于签订重大合同的公告", "positive"),
    ("progress_unlinked", "关于签订重大合同的进展公告", "positive"),
    ("resolution_unlinked", "关于立案调查结案的公告", "risk"),
))
@pytest.mark.parametrize("terminal", (False, True))
def test_unread_or_unextractable_body_keeps_previous_required_review(reason: str, title: str, scope: str, terminal: bool) -> None:
    previous = CompanyReviewReason(scope, reason, "contract", _NOW, "原有证据依赖待核验")
    document = replace(_document("", title), checked=False, terminal_failure=terminal)
    result = ExtractCompanyEvents(
        (document,), _NOW, CompanyEventPolicyFixture(), previous_reviews=(previous,)
    )
    assert previous in result.reviews
    assert {review.reason_code for review in result.reviews} == {
        reason, "extraction_unsupported" if terminal else "document_pending",
    }


def test_fully_checked_new_body_can_replace_previous_subject_review() -> None:
    previous = CompanyReviewReason("positive", "subject_unverified", "contract", _NOW, "原有主体待核验")
    document = _document("本公司已签订正式合同。合同金额为人民币2000万元（不含税）。" + _REVENUE)
    result = ExtractCompanyEvents(
        (document,), _NOW, CompanyEventPolicyFixture(), previous_reviews=(previous,)
    )
    assert result.reviews == () and result.positives[0].positive_level == 3


@pytest.mark.parametrize("context", ("", "本公司将及时履行信息披露义务，"))
def test_guarantee_without_verifiable_business_actor_does_not_create_issuer_placeholder(context: str) -> None:
    document = _document(_GUARANTEE + context + "相关合同事项仍在核实，实际签署方尚未明确。")
    result = ExtractCompanyEvents((document,), _NOW, CompanyEventPolicyFixture())
    assert result.positives == ()
    assert tuple(review.reason_code for review in result.reviews) == ("subject_unverified",)
    assert ExtractCompanyLinkCandidate(document, _NOW, CompanyEventPolicyFixture()) is None


@pytest.mark.parametrize("actor", ("本公司已签订正式合同", "公司于2026年11月4日签署正式合同", "本公司根据公告root说明，合同已生效"))
def test_explicit_issuer_contract_actor_is_preserved_without_subsidiary_keywords(actor: str) -> None:
    document = _document(_GUARANTEE + actor + "。合同已生效。合同金额为人民币2000万元（不含税）。" + _REVENUE)
    result = ExtractCompanyEvents((document,), _NOW, CompanyEventPolicyFixture())
    assert result.positives[0].subject_type is CompanySubject.ISSUER
    assert result.positives[0].positive_level == 3 and result.reviews == ()


@pytest.mark.parametrize("actor", (
    "本公司收到中标通知书，确认为项目中标人",
    "公司收到交易中心披露的中标结果",
    "公司被确定为项目中标人",
    "本公司为上述项目的中标人",
))
def test_issuer_bid_actor_is_preserved_but_does_not_prove_binding_contract(actor: str) -> None:
    document = _document(_GUARANTEE + actor + "。相关合同尚未签订，具体金额以正式签订合同为准。")
    result = ExtractCompanyEvents((document,), _NOW, CompanyEventPolicyFixture())
    assert result.positives[0].subject_type is CompanySubject.ISSUER
    assert result.positives[0].status is CompanyPositiveStatus.PLANNED
    assert result.positives[0].positive_level == 0 and result.reviews


@pytest.mark.parametrize("actor", ("本公司与交易对手经协商一致", "本公司决定", "本公司经协商"))
def test_explicit_issuer_termination_keeps_existing_same_event_cancellation(actor: str) -> None:
    original = _document("本公司已签订正式合同。合同金额为人民币2000万元（不含税）。" + _REVENUE)
    previous = ExtractCompanyEvents((original,), _NOW, CompanyEventPolicyFixture())
    assert previous.positives[0].positive_level == 3
    cancellation = _document(actor + "终止公告contract对应的合同。", "关于终止重大合同的公告")
    cancellation = replace(cancellation, disclosure=replace(cancellation.disclosure, identity="cancel"))
    result = ExtractCompanyEvents(
        (cancellation,), _NOW, CompanyEventPolicyFixture(), previous_positives=previous.positives
    )
    assert len(result.positives) == 1 and result.positives[0].event_id == previous.positives[0].event_id
    assert result.positives[0].status is CompanyPositiveStatus.CANCELLED
    assert result.positives[0].positive_level == 3 and result.reviews == ()
    assert result.positives[0].cancelled_at == cancellation.disclosure.published_at
    assert result.positives[0].measurements == previous.positives[0].measurements
    assert result.positives[0].level_milestones == previous.positives[0].level_milestones
    candidate = ExtractCompanyLinkCandidate(cancellation, _NOW, CompanyEventPolicyFixture())
    assert candidate is not None
    pending = CompanyReviewReason("positive", "cancellation_unlinked", "cancel", _NOW, "终止待关联")
    linked = ResolveCompanyLinks((candidate,), (), previous.positives, (pending,), CompanyEventPolicyFixture())
    assert len(linked.positives) == 1 and linked.positives[0].status is CompanyPositiveStatus.CANCELLED
    assert linked.positives[0].positive_level == 3 and linked.reviews == ()
    assert linked.positives[0].cancelled_at == cancellation.disclosure.published_at
    assert linked.positives[0].measurements == previous.positives[0].measurements
    assert linked.positives[0].level_milestones == previous.positives[0].level_milestones
    facts = CompanyFacts(
        code="002174",
        financial=CompanyFinancial("002174", _NOW.date(), _NOW, 1, 5, 25, 25, 25, 10, 100, 90),
        financial_state=CompanyDataState.COMPLETE,
        pledge=CompanyRatio("002174", _NOW.date(), 0),
        pledge_state=CompanyDataState.COMPLETE,
        unlock_schedule=(),
        unlock_checked_at=_NOW,
        unlock_coverage_end=_NOW.date() + timedelta(days=180),
        unlock_state=CompanyDataState.COMPLETE,
        risk_events=(),
        positive_events=previous.positives,
        review_reasons=(),
        pending_document_count=0,
        announcement_coverage_start=_NOW.date() - timedelta(days=365),
        announcement_coverage_end=_NOW.date(),
        announcement_state=CompanyDataState.COMPLETE,
        checked_at=_NOW,
        published_at=_NOW,
    )
    policy = CompanyScoringPolicyFixture(enabled=True)
    calendar = CompanyScoringCalendar(tuple(
        (_NOW + timedelta(days=offset)).date() for offset in range(-5, 45)
        if (_NOW + timedelta(days=offset)).weekday() < 5
    ))
    for events, expected_bonus in ((previous.positives, 3), (result.positives, 0), (linked.positives, 0)):
        context = BuildCompanyScoringContext(replace(facts, positive_events=events), calendar, policy)
        adjustments = AssessCompanyAdjustments(
            context, policy, "d25", _NOW, financial_scores=CompanyFinancialScores(100, 100, 100)
        )
        ledger = BuildScoreLedger(
            ScoreLedgerInput(80, adjustments, adjustments.risks), strategy="d25", policy=policy, risk_cap=25
        )
        assert ledger.applied_bonus == expected_bonus
        if not expected_bonus:
            assert tuple(item.reason for item in adjustments.bonuses if item.kind == "positive_bonus") == ("event_cancelled",)
