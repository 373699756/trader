"""Reduction completion needs the same plan/subject and keeps its risk tail."""

from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Literal

import pytest

from tests.helpers.download.company.company_score_ledger import AT, CompanyEventPolicy, _document
from trader.download.domain.company_event_extraction import (
    CompanyExtractedEvents,
    ExtractCompanyEvents,
    ExtractCompanyLinkCandidate,
    ResolveCompanyLinks,
)
from trader.download.domain.company_events import CompanyEventStatus, CompanyRiskEvent, ReductionPlan
from trader.download.domain.company_evidence import CompanyDocument
from trader.recommendation.domain.scoring.company import IsCompanyRiskEffective

_PLAN = (
    "本公司控股股东拟减持股份，占公司总股本的比例为2%。"
    "减持期间为2026年10月28日至2026年11月28日。"
)


def _previous_risk() -> CompanyRiskEvent:
    event, = ExtractCompanyEvents(
        (_document("plan", "控股股东减持计划公告", _PLAN),), AT, CompanyEventPolicy()
    ).risks
    return event


def _close(
    closure: CompanyDocument,
    event: CompanyRiskEvent,
    path: Literal["direct", "linked"],
) -> CompanyExtractedEvents:
    at = closure.disclosure.published_at
    policy = CompanyEventPolicy()
    if path == "direct":
        return ExtractCompanyEvents((closure,), at, policy, previous_risks=(event,))
    pending = ExtractCompanyEvents((closure,), at, policy)
    candidate = ExtractCompanyLinkCandidate(closure, at, policy)
    return ResolveCompanyLinks(
        (candidate,) if candidate is not None else (),
        (event,),
        (),
        pending.reviews,
        policy,
    )


@pytest.mark.parametrize("path", ("direct", "linked"))
@pytest.mark.parametrize(
    ("title", "conclusion"),
    (
        ("控股股东减持计划期限届满公告", "本次减持计划期限已届满，未减持股份"),
        ("控股股东减持计划实施完毕公告", "本次减持计划已实施完毕"),
        ("控股股东减持计划完成公告", "本次减持计划已完成"),
    ),
)
def test_same_plan_completion_keeps_dates_ratio_and_ninety_day_tail(path, title, conclusion):
    previous = _previous_risk()
    close_at = AT + timedelta(days=40)
    closure = _document("end", title, "本公司控股股东根据公告plan，" + conclusion + "。", close_at)

    result = _close(closure, previous, path)

    event, = result.risks
    assert event.event_id == previous.event_id
    assert event.status is CompanyEventStatus.RESOLVED
    assert event.resolved_at == close_at
    assert event.expires_at == close_at + timedelta(days=90)
    assert event.reduction_plan == replace(previous.reduction_plan, completion_disclosed_at=close_at)
    assert event.evidence_ids == ("plan", "end")
    assert IsCompanyRiskEffective(event, close_at + timedelta(days=90) - timedelta(microseconds=1))
    assert not IsCompanyRiskEffective(event, close_at + timedelta(days=90))
    assert not result.reviews

    repeated = _close(
        replace(closure, disclosure=replace(closure.disclosure, published_at=close_at + timedelta(days=5))),
        event,
        path,
    )
    assert repeated.risks[0].resolved_at == close_at
    assert repeated.risks[0].expires_at == event.expires_at


@pytest.mark.parametrize("path", ("direct", "linked"))
@pytest.mark.parametrize(
    ("title", "body"),
    (
        ("控股股东减持计划期限届满公告", "本公司控股股东根据公告other，本次减持计划期限已届满。"),
        ("实际控制人减持计划期限届满公告", "本公司实际控制人根据公告plan，本次减持计划期限已届满。"),
        ("控股股东减持计划期限届满公告", "本公司控股股东甲及乙根据公告plan，本次减持计划期限已届满。"),
        ("控股股东减持计划期限届满公告", "本公司控股股东甲、乙根据公告plan，仅甲的减持计划期限已届满。"),
        ("控股股东减持计划期限届满公告", "本公司控股股东根据公告plan，本次减持计划期限尚未届满。"),
        ("控股股东减持计划期限届满公告", "本公司控股股东根据公告plan，预计本次减持计划期限届满后披露结果。"),
        ("控股股东减持计划完成公告", "本公司控股股东根据公告plan，本次减持计划尚未完成。"),
        ("控股股东减持计划完成公告", "本公司控股股东根据公告plan，本次减持计划已完成部分减持，剩余股份继续实施。"),
        ("控股股东减持计划完成公告", "本公司控股股东根据公告plan，本次减持计划完成时间尚不确定。"),
        ("控股股东减持计划期限届满公告", "控股股东甲根据公告plan，甲本次减持计划期限已届满，股东乙仍继续实施同一计划。"),
    ),
)
def test_unlinked_other_subject_multiple_holders_and_future_completion_cannot_close_risk(path, title, body):
    previous = _previous_risk()
    closure = _document("end", title, body, AT + timedelta(days=5))

    result = _close(closure, previous, path)

    assert result.risks == (previous,)
    assert any(review.reason_code == "resolution_unlinked" for review in result.reviews)


@pytest.mark.parametrize("path", ("direct", "linked"))
def test_completion_cannot_close_another_case_even_with_original_evidence_reference(path):
    previous = replace(_previous_risk(), case_id="〔2026〕减持1号")
    closure = _document(
        "end", "控股股东减持计划期限届满公告",
        "本公司控股股东根据公告plan，〔2026〕减持2号的减持计划期限已届满。", AT + timedelta(days=5),
    )
    assert _close(closure, previous, path).risks == (previous,)


@pytest.mark.parametrize("path", ("direct", "linked"))
def test_completion_referencing_two_plans_cannot_choose_or_close_both(path):
    first = replace(_previous_risk(), event_id="plan1:major_shareholder_reduction", evidence_ids=("plan1",))
    second = replace(_previous_risk(), event_id="plan2:major_shareholder_reduction", evidence_ids=("plan2",))
    at = AT + timedelta(days=5)
    closure = _document(
        "end", "控股股东减持计划期限届满公告",
        "本公司控股股东根据公告plan1及公告plan2，本次减持计划期限已届满。", at,
    )
    policy = CompanyEventPolicy()
    if path == "direct":
        result = ExtractCompanyEvents((closure,), at, policy, previous_risks=(first, second))
    else:
        pending = ExtractCompanyEvents((closure,), at, policy)
        candidate = ExtractCompanyLinkCandidate(closure, at, policy)
        result = ResolveCompanyLinks(
            (candidate,) if candidate is not None else (),
            (first, second),
            (),
            pending.reviews,
            policy,
        )
    assert result.risks == (first, second)
    assert any(review.reason_code == "resolution_unlinked" for review in result.reviews)


@pytest.mark.parametrize("path", ("direct", "linked"))
def test_completion_disclosed_before_original_plan_cannot_close_it(path):
    previous = _previous_risk()
    closure = _document(
        "end", "控股股东减持计划期限届满公告",
        "本公司控股股东根据公告plan，本次减持计划期限已届满。", AT - timedelta(days=1),
    )
    result = _close(closure, previous, path)
    assert result.risks == (previous,)
    assert any(review.reason_code == "resolution_unlinked" for review in result.reviews)


@pytest.mark.parametrize("guard", ("isolated", "linked", "shared"))
def test_exact_expiry_replay_only_retires_an_isolated_notice_derived_risk(guard):
    previous = _previous_risk()
    at = AT + timedelta(days=5)
    closure = _document(
        "end", "控股股东减持计划期限届满公告",
        "本公司控股股东根据公告missing，本次减持计划期限已届满。", at,
    )
    mistaken = replace(
        previous,
        event_id="end:major_shareholder_reduction",
        first_disclosed_at=at,
        last_disclosed_at=at,
        effective_at=at,
        expires_at=None,
        summary=closure.disclosure.title,
        evidence_ids=("end",),
        reduction_plan=ReductionPlan(),
    )
    others = (previous,)
    if guard == "linked":
        mistaken = replace(mistaken, evidence_ids=("original", "end"))
    elif guard == "shared":
        others = (previous, replace(previous, event_id="other", evidence_ids=("other", "end")))

    result = ExtractCompanyEvents((closure,), at, CompanyEventPolicy(), previous_risks=(*others, mistaken))

    expected = others if guard == "isolated" else (*others, mistaken)
    assert set(result.risks) == set(expected)
    assert any(review.reason_code == "resolution_unlinked" for review in result.reviews)


def test_unlinked_expiry_notice_creates_review_instead_of_another_reduction_plan():
    closure = _document(
        "end", "关于持股5%以上股东减持计划期限届满暨未减持股份的公告",
        "本公司持股5%以上股东就公告original披露情况，本次减持计划期限已届满，未减持股份。",
    )

    result = ExtractCompanyEvents((closure,), AT, CompanyEventPolicy())

    assert result.risks == ()
    assert any(review.reason_code == "resolution_unlinked" for review in result.reviews)


def test_expiry_reminder_with_no_completed_plan_cannot_retire_its_old_risk():
    title = "控股股东减持计划期限届满前提示公告"
    reminder = _document("reminder", title, "本公司控股股东的本次减持计划期限尚未届满，剩余期间继续实施。")
    previous = replace(
        _previous_risk(),
        event_id="reminder:major_shareholder_reduction",
        evidence_ids=("reminder",),
        summary=title,
    )

    result = ExtractCompanyEvents((reminder,), AT, CompanyEventPolicy(), previous_risks=(previous,))

    assert result.risks == (previous,)


@pytest.mark.parametrize(
    "period",
    (
        "减持期间为2026年11月4日至2027年2月3日。",
        "减持期间：自本公告披露之日起十五个交易日后的三个月内（即2026年11月4日至2027年2月3日）。",
        "减持期间为自本公告披露之日起十五个交易日后的三个月内(即2026年11月4日至2027年2月3日)。",
        "5.减持期间：自减持计划披露之日起十五个交易日后的三个月内（即2026年11月4日至2027年2月3日）。",
        "减持期间（即2026年11月4日至2027年2月3日）。减持计划期间为2026年11月4日至2027年2月3日。",
    ),
)
def test_explicit_parenthesized_reduction_period_preserves_total_capital_ratio(period):
    plan = _document(
        "plan", "控股股东减持计划公告", "本公司控股股东拟减持股份，占公司总股本的比例为2%。" + period,
    )
    event, = ExtractCompanyEvents((plan,), AT, CompanyEventPolicy()).risks
    assert event.reduction_plan.planned_start_date == date(2026, 11, 4)
    assert event.reduction_plan.planned_end_date == date(2027, 2, 3)
    assert event.reduction_plan.planned_ratio_pct == 2
    assert event.expires_at == datetime(2027, 2, 3, 23, 59, 59, 999999, tzinfo=AT.tzinfo) + timedelta(days=90)


@pytest.mark.parametrize(
    "period",
    (
        "减持期间自本公告披露之日起十五个交易日后的三个月内。其它事项（即2026年11月4日至2027年2月3日）。",
        "减持期间（即2026年11月31日至2027年2月3日）。",
        "减持期间（即2027年2月3日至2026年11月4日）。",
        "减持期间（即2026年11月4日至2027年2月3日）；另一减持期间为2026年12月1日至2027年3月1日。",
        "减持期间另行确定，员工持股计划期间（即2026年11月4日至2027年2月3日）。",
    ),
)
def test_ambiguous_unrelated_or_invalid_period_does_not_invent_dates(period):
    plan = _document(
        "plan", "控股股东减持计划公告", "本公司控股股东拟减持股份，占公司总股本的比例为2%。" + period,
    )
    event, = ExtractCompanyEvents((plan,), AT, CompanyEventPolicy()).risks
    assert event.reduction_plan.planned_start_date is None
    assert event.reduction_plan.planned_end_date is None
    assert event.reduction_plan.planned_ratio_pct == 2
    assert event.expires_at is None


def test_period_only_amendment_does_not_erase_an_existing_ratio():
    previous = _previous_risk()
    at = AT + timedelta(days=1)
    update = _document(
        "update", "控股股东减持计划变更公告",
        "本公司控股股东根据公告plan调整减持计划，减持期间：自本公告披露之日起十五个交易日后的"
        "三个月内（即2026年11月4日至2027年2月3日）。", at,
    )
    result = ExtractCompanyEvents((update,), at, CompanyEventPolicy(), previous_risks=(previous,))
    event, = result.risks
    assert event.event_id == previous.event_id
    assert event.reduction_plan.planned_start_date == date(2026, 11, 4)
    assert event.reduction_plan.planned_end_date == date(2027, 2, 3)
    assert event.reduction_plan.planned_ratio_pct == 2
