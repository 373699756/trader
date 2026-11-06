"""Exact source locators prove fields without expanding event or scoring rules."""

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.download.domain.company_event_extraction import ExtractCompanyFieldEvidence, ExtractCompanyLinkCandidate
from trader.download.domain.company_events import CompanyEventPolicy, CompanySubject, RepurchasePurpose
from trader.download.domain.company_evidence import CompanyDocument, Disclosure


_AT = datetime(2026, 11, 6, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
_POLICY = CompanyEventPolicy((0.1, 0.3, 0.5), (5.0, 10.0, 20.0), 365, 90, 180)


def _document(body, title="关于首次回购股份的公告"):
    return CompanyDocument(
        Disclosure("cninfo", "proof-input", ("600001",), title, _AT, "", "current-directory-version"),
        body, True,
    )


def test_multiline_actual_ratio_has_original_line_quote_and_full_text_hash():
    text = (
        "证券代码：600001\n回购公告\n本公司首次回购股份100万股，占公司\n"
        "总股本的比例为0.10\n%。\n回购股份用于员工持股计划。"
    )
    document = _document(text)
    result = ExtractCompanyFieldEvidence(document, _AT, _POLICY)
    actual = next(item for item in result if item.field == "actual_pct")
    assert actual.state == "verified" and actual.value == 0.1 and actual.applicable
    assert actual.code == "600001" and actual.event_id == "proof-input:share_repurchase"
    evidence, = actual.evidence
    assert (evidence.start_line, evidence.end_line) == (3, 5)
    assert evidence.quote == "首次回购股份100万股，占公司\n总股本的比例为0.10\n%"
    assert evidence.quote in text and evidence.body_hash == hashlib.sha256(text.encode()).digest()
    assert evidence.unit == "percent" and evidence.record_hash == "current-directory-version"
    assert evidence.subject is CompanySubject.ISSUER
    purpose = next(item for item in result if item.field == "repurchase_purpose")
    assert purpose.value == "employee_plan" and purpose.evidence[0].quote == "用于员工持股计划"
    assert purpose.evidence[0].start_line == purpose.evidence[0].end_line == 6


def test_checkbox_proof_preserves_selected_mark_and_does_not_bless_empty_alternatives():
    text = (
        "本公司首次回购股份100万股，占公司总股本的0.062%。回购用途：\n"
        "□减少注册资本\n□用于员工持股计划或股权激励\n"
        "√用于转换公司可转债\n□为维护公司价值及股东权益"
    )
    results = ExtractCompanyFieldEvidence(_document(text), _AT, _POLICY)
    purpose = next(item for item in results if item.field == "repurchase_purpose")
    assert purpose.value == "other"
    assert purpose.evidence[0].quote == "√用于转换公司可转债"
    assert purpose.evidence[0].start_line == purpose.evidence[0].end_line == 4


@pytest.mark.parametrize("separator", ("\r", "\r\n", "\f"))
def test_original_line_locator_uses_the_same_splitlines_boundaries_as_material_readers(separator):
    text = separator.join((
        "证券代码：600001", "回购公告", "本公司首次回购股份占公司", "总股本比例为0.1", "%",
    ))
    conclusion, = ExtractCompanyFieldEvidence(_document(text), _AT, _POLICY)
    assert (conclusion.evidence[0].start_line, conclusion.evidence[0].end_line) == (3, 5)
    assert conclusion.evidence[0].quote in text


def test_qualifying_holder_actual_ratio_uses_the_existing_increase_parser():
    document = _document("控股股东累计已增持股份100万股，占公司总股本的0.3%。", "控股股东增持股份公告")
    result, = ExtractCompanyFieldEvidence(document, _AT, _POLICY)
    assert result.field == "actual_pct" and result.value == 0.3
    assert result.event_id == "proof-input:shareholder_increase"
    assert result.evidence[0].subject is CompanySubject.CONTROLLING_SHAREHOLDER
    assert result.evidence[0].quote == "累计已增持股份100万股，占公司总股本的0.3%"


@pytest.mark.parametrize("change", ["future", "unofficial", "unchecked", "multiple_codes", "news_kind", "terminal"])
def test_future_unverified_source_incomplete_and_multiple_issuers_have_no_proof(change):
    document = _document("本公司首次回购股份100万股，占公司总股本的0.1%，用于注销。")
    if change == "future":
        document = replace(document, disclosure=replace(document.disclosure, published_at=_AT + timedelta(seconds=1)))
    elif change == "unofficial":
        document = replace(document, disclosure=replace(document.disclosure, source="news"))
    elif change == "unchecked":
        document = replace(document, checked=False)
    elif change == "multiple_codes":
        document = replace(document, disclosure=replace(document.disclosure, codes=("600001", "600002")))
    elif change == "news_kind":
        document = replace(document, disclosure=replace(document.disclosure, kind="news"))
    else:
        document = replace(document, terminal_failure=True)
    assert ExtractCompanyFieldEvidence(document, _AT, _POLICY) == ()


@pytest.mark.parametrize("statement", (
    "本公司拟首次回购股份，占公司总股本的比例为0.3%。",
    "本公司尚未实际首次回购股份，占公司总股本的比例为0.3%。",
    "本公司首次回购股份占公司总股本比例为0.1%；首次回购股份占公司总股本比例为0.2%。",
    "本公司首次回购股份占公司总股本比例为0.1%；首次回购股份占公司总股本比例为0.1%。",
))
def test_plan_negation_conflicting_or_unlocatable_repeated_actual_statement_has_no_proof(statement):
    assert ExtractCompanyFieldEvidence(_document(statement), _AT, _POLICY) == ()


def test_other_subject_measurement_cannot_become_issuer_repurchase_proof():
    document = _document(
        "本公司董事会保证披露真实。公司将在回购期限内根据市场情况择机作出回购决策。"
        "控股股东累计已增持股份占公司总股本比例为2%。",
        "关于回购股份的回购报告书",
    )
    assert ExtractCompanyFieldEvidence(document, _AT, _POLICY) == ()


@pytest.mark.parametrize(("title", "body"), (
    ("公司增持股份公告", "本公司累计已增持股份占公司总股本比例为0.3%。"),
    ("控股股东回购股份公告", "控股股东累计已回购股份占公司总股本比例为0.3%。"),
))
def test_action_actor_must_match_the_supported_field_subject(title, body):
    assert ExtractCompanyFieldEvidence(_document(body, title), _AT, _POLICY) == ()


def test_explicit_header_for_another_security_cannot_certify_the_input_code():
    document = _document("证券代码：600002\n本公司首次回购股份占公司总股本比例为0.3%，用于注销。")
    assert ExtractCompanyFieldEvidence(document, _AT, _POLICY) == ()


def test_statement_exceeding_the_excerpt_budget_keeps_the_field_unverified():
    document = _document("本公司首次回购股份" + "\n" * 1100 + "占公司总股本比例为0.3%。")
    assert ExtractCompanyFieldEvidence(document, _AT, _POLICY) == ()


@pytest.mark.parametrize("purpose", (
    "√减少注册资本√用于员工持股计划",
    "√用于员工持股计划或股权激励",
    "回购股份不用于员工持股计划",
    "本公司不再将回购股份用于员工持股计划",
    "本次回购的用途不是用于员工持股计划",
    "不得将本次回购股份用于员工持股计划",
    "董事会另行讨论员工持股计划",
    "用于注销；用于注销",
))
def test_ambiguous_denied_or_unrelated_purpose_is_not_promoted_to_verified(purpose):
    document = _document("本公司首次回购股份100万股，占公司总股本的0.1%。" + purpose)
    before = ExtractCompanyLinkCandidate(document, _AT, _POLICY)
    results = ExtractCompanyFieldEvidence(document, _AT, _POLICY)
    assert [item.field for item in results] == ["actual_pct"]
    assert ExtractCompanyLinkCandidate(document, _AT, _POLICY) == before


def test_verified_employee_purpose_does_not_make_existing_event_eligible_for_points():
    document = _document("本公司首次回购股份100万股，占公司总股本的0.3%。回购股份用于员工持股计划。")
    results = ExtractCompanyFieldEvidence(document, _AT, _POLICY)
    candidate = ExtractCompanyLinkCandidate(document, _AT, _POLICY)
    assert {result.field for result in results} == {"actual_pct", "repurchase_purpose"}
    assert candidate.positive.measurements.purpose is RepurchasePurpose.EMPLOYEE_PLAN
    assert candidate.positive.positive_level == 0


def test_cancellation_and_unzoned_observation_cannot_certify_fields():
    document = _document("本公司已回购股份占公司总股本比例为0.3%，用于注销。", "关于取消回购股份的公告")
    assert ExtractCompanyFieldEvidence(document, _AT, _POLICY) == ()
    with pytest.raises(ValueError, match="company_field_evidence_time_invalid"):
        ExtractCompanyFieldEvidence(document, _AT.replace(tzinfo=None), _POLICY)
