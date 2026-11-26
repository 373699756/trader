"""A regional warning requires a current signed decision against the issuer."""

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from trader.download.domain.company_event_extraction import ExtractCompanyEvents
from trader.download.domain.company_events import CompanyRiskType, CompanySubject
from trader.download.domain.company_evidence import CompanyDocument


_TITLE = "关于公司及相关人员收到广东证监局警示函的公告"
_ISSUER = "广州示例股份有限公司"
_RECEIPT = (
    _ISSUER + "（以下简称“公司”）于近日收到中国证券监督管理委员会广东监管局出具的"
    "《关于对广州示例股份有限公司、甲某采取出具警示函措施的决定》（[2026]143号）。"
)
_OPERATIVE = "根据有关规定，我局决定对示例股份、甲某采取出具警示函的行政监管措施。"
_SIGNATURE = "特此公告。广州示例股份有限公司董事会2026年10月18日"
_BODY = "证券代码：600001证券简称：示例股份公告编号：2026-038。" + _TITLE + "。本公司保证信息披露真实。" + _RECEIPT + _OPERATIVE + _SIGNATURE


def _document(body=_BODY, title=_TITLE):
    return CompanyDocument(fixtures._disclosure("warning", title), body, True)


@pytest.mark.parametrize("recipient", ["示例股份、甲某", "你们", "广州示例股份有限公司、甲某"])
def test_current_warning_binds_issuer_authority_case_and_first_disclosure(recipient):
    doc = _document(_BODY.replace("对示例股份、甲某", "对" + recipient))
    policy = fixtures.CompanyEventPolicy()
    assert not ExtractCompanyEvents((doc,), fixtures.NOW - timedelta(microseconds=1), policy).risks
    events = ExtractCompanyEvents((doc,), fixtures.NOW, policy)
    event, = events.risks
    assert event.risk_type is CompanyRiskType.REGULATORY_WARNING and event.risk_level == 1
    assert event.subject_type is CompanySubject.ISSUER and event.case_id == "广东证监局〔2026〕143号"
    assert event.evidence_ids == ("warning",) and event.effective_at == fixtures.NOW
    assert event.expires_at == fixtures.NOW + timedelta(days=365) and not events.reviews
    repeated = ExtractCompanyEvents((doc,), fixtures.NOW, policy, previous_risks=events.risks)
    assert repeated.risks == events.risks


@pytest.mark.parametrize("old,new", [
    ("对广州示例股份有限公司、甲某采取", "对甲某采取"),
    ("对广州示例股份有限公司、甲某采取", "对广州示例股份有限公司子公司、甲某采取"),
    ("对广州示例股份有限公司、甲某采取", "对广州示例股份有限公司原董事长甲某采取"),
    ("对示例股份、甲某采取", "对甲某采取"),
    ("广东监管局出具", "北京监管局出具"),
    ("于近日收到", "曾于2020年收到"),
    ("于近日收到", "于近日未收到"),
    ("于近日收到", "预计可能收到"),
    ("，我局决定", "，如我局决定"),
    ("，我局决定", "，我局并未决定"),
    ("我局决定", "相关人员认为我局决定"),
    ("2026年10月18日", "2025年10月18日"),
    ("证券代码：600001", "证券代码：600002"),
    ("[2026]143号", "编号待核"),
    (_OPERATIVE, _OPERATIVE + "另案，我局决定对甲某采取出具警示函的行政监管措施。"),
    (_RECEIPT, _RECEIPT + _RECEIPT),
    (_OPERATIVE, _OPERATIVE + "上述决定已撤销。"),
    (_OPERATIVE, _OPERATIVE + "所述警示函不涉及本公司。"),
    (_RECEIPT, "以下引述历史案例。" + _RECEIPT),
    ("。广州示例股份有限公司（以下简称", "。甲公司披露广州示例股份有限公司（以下简称"),
    ("特此公告。广州示例股份有限公司", "特此公告。广州其他股份有限公司"),
    (_BODY, "本公司公告：" + _TITLE),
])
def test_warning_declines_unbound_hypothetical_superseded_or_historical_text(old, new):
    events = ExtractCompanyEvents((_document(_BODY.replace(old, new)),), fixtures.NOW, fixtures.CompanyEventPolicy())
    assert not events.risks
    assert any(review.scope == "risk" for review in events.reviews)


def test_warning_case_numbers_are_local_to_authority_and_title_only_never_proves_a_decision():
    first = _document()
    second = _document(_BODY.replace("广东", "北京"), _TITLE.replace("广东", "北京"))
    second = replace(second, disclosure=replace(second.disclosure, identity="other-region"))
    events = ExtractCompanyEvents((first, second), fixtures.NOW, fixtures.CompanyEventPolicy())
    assert {risk.case_id for risk in events.risks} == {"广东证监局〔2026〕143号", "北京证监局〔2026〕143号"}
    pending = ExtractCompanyEvents((replace(first, checked=False),), fixtures.NOW, fixtures.CompanyEventPolicy())
    assert not pending.risks and pending.reviews
