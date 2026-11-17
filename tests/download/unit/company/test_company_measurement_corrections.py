"""A disposal's own incorrect measurement may be withdrawn without losing history."""

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.download.component.company.test_company_sync_round import _SCOPE
from tests.helpers.download.configuration import CompanyEventPolicyFixture
from trader.download.domain.company_event_extraction import ExtractCompanyLinkCandidate
from trader.download.domain.company_events import (
    CompanyDataState, CompanyLevelMilestone, CompanyPositiveStatus, CompanyReviewReason, RepurchaseMeasurements,
)
from trader.download.domain.company_evidence import CompanyDocument, Disclosure
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_measurement_corrections import CorrectCompanyRepurchaseDisposalMeasurements


def _input():
    at = _SCOPE.cutoff - timedelta(days=1)
    disclosure = Disclosure("cninfo", "disposal", ("600001",), "关于注销回购股份的公告", at,
        "https://example.test/disposal.pdf", "a" * 64)
    text = "证券代码：600001\n示例股份有限公司\n关于注销回购股份的公告\n本公司及董事会全体成员保证信息披露的内容真实准确完整。\n本公司于2023年已累计回购股份100万股，占公司总股本的0.82%。上述股份现拟予以注销，不产生新的股份回购实施。"
    document = CompanyDocument(disclosure, text, True)
    candidate = ExtractCompanyLinkCandidate(document, _SCOPE.cutoff, CompanyEventPolicyFixture())
    assert candidate is not None and candidate.positive is not None
    original = replace(candidate.positive, status=CompanyPositiveStatus.COMPLETED,
        measurements=RepurchaseMeasurements(0.82, candidate.positive.measurements.purpose), positive_level=1,
        level_milestones=(CompanyLevelMilestone(1, at, 0.82, "disposal"),))
    facts = CompanyFacts("600001", None, CompanyDataState.UNAVAILABLE, None, CompanyDataState.UNAVAILABLE,
        (), None, None, CompanyDataState.UNAVAILABLE, (), (original,),
        (CompanyReviewReason("positive", "implementation_or_measurement_unverified", "disposal", at, "待核实"),),
        0, None, None, CompanyDataState.PARTIAL, at, at, latest_financial=None)
    return facts, document


def test_own_measurement_review_does_not_block_exact_disposal_correction_and_history_is_preserved():
    facts, document = _input()
    result = CorrectCompanyRepurchaseDisposalMeasurements(facts, (document,), _SCOPE.cutoff, CompanyEventPolicyFixture())
    old, new = facts.positive_events[0], result.positive_events[0]
    assert new.measurements.actual_ratio_pct is None and new.positive_level == 0 and new.level_milestones == ()
    assert (new.event_id, new.evidence_ids, new.first_disclosed_at, new.last_disclosed_at) == (
        old.event_id, old.evidence_ids, old.first_disclosed_at, old.last_disclosed_at)
    assert result.risk_events == facts.risk_events and result.review_reasons == facts.review_reasons
    assert CorrectCompanyRepurchaseDisposalMeasurements(result, (document,), _SCOPE.cutoff, CompanyEventPolicyFixture()) is result


@pytest.mark.parametrize("case", ("shared_event", "risk_review", "historical_link", "multi_issuer", "future", "unverified"))
def test_shared_or_unqualified_evidence_never_withdraws_an_independent_measurement(case):
    facts, document = _input()
    if case == "shared_event":
        facts = replace(facts, positive_events=(*facts.positive_events, replace(facts.positive_events[0], event_id="other")))
    elif case == "risk_review":
        facts = replace(facts, review_reasons=(*facts.review_reasons, CompanyReviewReason("risk", "conclusion_unverified", "disposal", document.disclosure.published_at, "独立风险待核")))
    elif case == "historical_link":
        facts = replace(facts, positive_events=(replace(facts.positive_events[0], first_disclosed_at=document.disclosure.published_at - timedelta(days=100)),))
    elif case == "multi_issuer":
        document = replace(document, disclosure=replace(document.disclosure, codes=("600001", "600002")))
    elif case == "future":
        document = replace(document, disclosure=replace(document.disclosure, published_at=_SCOPE.cutoff + timedelta(days=1)))
    else:
        document = replace(document, checked=False)
    assert CorrectCompanyRepurchaseDisposalMeasurements(facts, (document,), _SCOPE.cutoff, CompanyEventPolicyFixture()) is facts
