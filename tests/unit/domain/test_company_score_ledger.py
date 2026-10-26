"""Money-independent score arithmetic, event verification and temporal boundaries."""

import json
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.download.domain.company_event_extraction import ExtractCompanyEvents
from trader.download.domain.company_events import CompanyDataState, CompanyEventPolicy
from trader.download.domain.company_evidence import CompanyDocument, CompanyFinancial, CompanyRatio, Disclosure
from trader.download.domain.company_facts import CompanyFacts
from trader.download.infra.company_facts_codec import (
    PUBLICATION_COLUMNS,
    DecodeCompanyPublication,
    EncodeCompanyPublication,
)
from trader.recommendation.domain.evidence.review import RiskFact
from trader.recommendation.domain.publication.score_ledger import ScoreAdjustmentItem, ScoreLedgerPayload
from trader.recommendation.domain.scoring.company import (
    AssessCompanyAdjustments,
    BuildCompanyScoringContext,
    BuildScoreLedger,
    CompanyAdjustments,
    CompanyFinancialScores,
    CompanyRiskActive,
    CompanyScoringPolicy,
    ScoreLedgerInput,
)
from trader.recommendation.infra.persistence.score_ledger_codec import DecodeScoreLedger

AT = datetime(2026, 10, 27, 10, tzinfo=ZoneInfo("Asia/Shanghai"))


def _adjustment(points):
    return CompanyAdjustments(
        (ScoreAdjustmentItem("buyback", "positive_bonus", "实际回购", 3, points, 0, "eligible"),), (), 0, AT.date()
    )


@pytest.mark.parametrize(
    ("base", "bonus", "penalty", "expected_bonus", "expected_risk", "final"),
    [
        (80, 0, 0, 0, 0, 80),
        (80, 3, 0, 3, 0, 83),
        (80, 8, 40, 5, 25, 60),
        (99, 5, 8, 1, 8, 92),
        (2, 0, 8, 0, 2, 0),
        (99.995, 5, 0, 0.005, 0, 100),
    ],
)
def test_score_limits_keep_rule_and_actual_values_distinct(base, bonus, penalty, expected_bonus, expected_risk, final):
    risk = RiskFact("market", "market", "high", penalty, "structured", AT)
    ledger = BuildScoreLedger(
        ScoreLedgerInput(base, _adjustment(bonus), (risk,)),
        strategy="d25",
        policy=CompanyScoringPolicy(enabled=True),
        risk_cap=25,
    )
    assert ledger.applied_bonus == pytest.approx(expected_bonus)
    assert ledger.applied_risk_penalty == expected_risk
    assert ledger.local_score == final
    assert ledger.local_risk_penalty == min(25, penalty)
    assert DecodeScoreLedger(ScoreLedgerPayload(ledger)) == ledger


@pytest.mark.parametrize("penalty", [-1, float("nan"), float("inf")])
def test_invalid_penalty_never_becomes_a_reward(penalty):
    with pytest.raises(ValueError, match="penalty_invalid"):
        BuildScoreLedger(
            ScoreLedgerInput(80, _adjustment(0), (RiskFact("bad", "bad", "high", penalty, "structured", AT),)),
            strategy="tomorrow",
            policy=CompanyScoringPolicy(),
            risk_cap=25,
        )


def _document(identity, title, body, at=AT):
    return CompanyDocument(
        Disclosure("cninfo", identity, ("600001",), title, at, "https://example.test/notice.pdf", identity), body, True
    )


def test_verified_soft_risk_and_persistent_case_have_distinct_expiry():
    warning = _document("warning", "关于收到警示函的公告", "本公司收到证监会出具警示函的决定。")
    case = _document("case", "关于收到立案告知书的公告", "本公司收到证监会立案告知书〔2026〕调查1号。")
    result = ExtractCompanyEvents((warning, case), AT, CompanyEventPolicy())
    assert {event.risk_level for event in result.risks} == {1, 4}
    later = AT + timedelta(days=366)
    assert [event.risk_level for event in result.risks if CompanyRiskActive(event, later)] == [4]
    closure = _document("closure", "关于调查终结的公告", "本公司收到证监会〔2026〕调查1号通知，调查终结。", later)
    resolved = ExtractCompanyEvents((closure,), later, CompanyEventPolicy(), previous_risks=result.risks)
    assert not any(CompanyRiskActive(event, later) for event in resolved.risks)


def test_positive_progress_does_not_extend_existing_level_and_revision_suspends():
    first = _document("repurchase", "关于回购股份的公告", "本公司已实际回购股份，占公司总股本的比例为0.3%，用于注销。")
    result = ExtractCompanyEvents((first,), AT, CompanyEventPolicy())
    assert result.positives[0].positive_level == 2
    next_at = AT + timedelta(days=3)
    progress = _document(
        "progress",
        "关于回购股份进展的公告",
        "本公司根据公告repurchase，累计回购股份占公司总股本的比例为0.5%，用于注销。",
        next_at,
    )
    updated = ExtractCompanyEvents((progress,), next_at, CompanyEventPolicy(), previous_positives=result.positives)
    event = updated.positives[0]
    assert event.event_id == result.positives[0].event_id
    assert [(milestone.level, milestone.qualified_at) for milestone in event.level_milestones] == [
        (1, AT),
        (2, AT),
        (3, next_at),
    ]
    pending = ExtractCompanyEvents(
        (replace(progress, text="", checked=False),),
        next_at,
        CompanyEventPolicy(),
        previous_positives=updated.positives,
    )
    assert pending.positives[0].positive_level == 0
    assert pending.positives[0].not_eligible_reason == "revision_pending"


def test_title_only_positive_and_employee_buyback_never_receive_awards():
    planned = _document("planned", "关于回购股份的公告", "本公司拟回购股份，占公司总股本的比例为1%，用于注销。")
    employee = _document("employee", "关于回购股份的公告", "本公司累计回购股份占公司总股本的比例为1%，用于员工持股。")
    result = ExtractCompanyEvents((planned, employee), AT, CompanyEventPolicy())
    assert all(event.positive_level == 0 for event in result.positives)


def _facts(documents=(), at=AT):
    events = ExtractCompanyEvents(documents, at, CompanyEventPolicy())
    return CompanyFacts(
        "600001",
        CompanyFinancial("600001", at.date(), at, 1, 5, 25, 25, 25, 10, 100, 90),
        CompanyDataState.COMPLETE,
        CompanyRatio("600001", at.date(), 0),
        CompanyDataState.COMPLETE,
        (),
        at,
        at.date() + timedelta(days=180),
        CompanyDataState.COMPLETE,
        events.risks,
        events.positives,
        events.reviews,
        0,
        at.date() - timedelta(days=365),
        at.date(),
        CompanyDataState.COMPLETE,
        at,
        at,
    )


def _assess(facts, strategy="tomorrow", *, at=AT, enabled=True, calendar=True):
    policy = CompanyScoringPolicy(enabled=enabled)
    days = tuple((AT + timedelta(days=i)).date() for i in range(-5, 40) if (AT + timedelta(days=i)).weekday() < 5)
    context = BuildCompanyScoringContext(facts, days if calendar else (), policy)
    return AssessCompanyAdjustments(
        context, policy, strategy, at, financial_scores=CompanyFinancialScores(100, 100, 100)
    )


def test_financial_bonus_and_positive_groups_obey_strategy_caps_without_d25_double_count():
    docs = tuple(
        _document(identity, "关于回购股份的公告", "本公司已实际回购股份，占公司总股本的比例为0.5%，用于注销。")
        for identity in ("one", "two")
    )
    facts = _facts(docs)
    for strategy, requested, applied in (("tomorrow", 5, 2), ("d25", 3, 3)):
        adjustments = _assess(facts, strategy)
        ledger = BuildScoreLedger(
            ScoreLedgerInput(80, adjustments, adjustments.risks),
            strategy=strategy,
            policy=CompanyScoringPolicy(enabled=True),
            risk_cap=25,
        )
        assert ledger.requested_bonus == requested and ledger.applied_bonus == applied
        assert sum(item.reason == "same_group_lower" for item in ledger.items) == 1
        assert any(item.kind == "financial_bonus" for item in ledger.items) == (strategy == "tomorrow")


@pytest.mark.parametrize("mode", ("unknown", "soft_risk", "disabled"))
def test_unknown_coverage_and_active_risk_and_disabled_candidate_never_grant_rewards(mode):
    facts = (
        _facts((_document("warning", "关于收到警示函的公告", "本公司收到证监会出具警示函的决定。"),))
        if mode == "soft_risk"
        else _facts()
    )
    if mode == "unknown":
        facts = replace(facts, pledge_state=CompanyDataState.PARTIAL)
    adjustments = _assess(facts, enabled=mode != "disabled")
    assert not any(item.reason == "eligible" for item in adjustments.bonuses)
    assert sum(fact.penalty for fact in adjustments.risks) == (3 if mode == "soft_risk" else 0)


def test_positive_expiry_uses_trading_sessions_and_unknown_calendar_never_guesses():
    facts = _facts(
        (_document("one", "关于回购股份的公告", "本公司已实际回购股份，占公司总股本的比例为0.5%，用于注销。"),)
    )
    context = BuildCompanyScoringContext(
        facts,
        tuple((AT + timedelta(days=i)).date() for i in range(-5, 40) if (AT + timedelta(days=i)).weekday() < 5),
        CompanyScoringPolicy(),
    )
    until = context.windows[-1].tomorrow_until
    assert until.weekday() < 5 and until.hour == 15
    assert any(item.reason == "eligible" for item in _assess(facts, "d25", at=until + timedelta(seconds=1)).bonuses)
    assert not any(
        item.reason == "eligible"
        for item in _assess(facts, "tomorrow", at=until + timedelta(seconds=1)).bonuses
        if item.kind == "positive_bonus"
    )
    assert not any(item.reason == "eligible" for item in _assess(facts, "d25", calendar=False).bonuses)
    truncated = BuildCompanyScoringContext(
        facts,
        tuple((AT + timedelta(days=i)).date() for i in range(200, 240) if (AT + timedelta(days=i)).weekday() < 5),
        CompanyScoringPolicy(),
    )
    assert all(window.tomorrow_until is None and window.d25_until is None for window in truncated.windows)
    assert not any(item.reason == "eligible" for item in _assess(facts, "d25", at=AT - timedelta(seconds=1)).bonuses)


def test_pending_original_cannot_be_reenabled_by_unchanged_progress():
    first = _document("one", "关于回购股份的公告", "本公司已实际回购股份，占公司总股本的比例为0.3%，用于注销。")
    progress = _document(
        "two",
        "关于回购股份进展的公告",
        "本公司根据公告one，累计回购股份占公司总股本的比例为0.5%，用于注销。",
        AT + timedelta(days=1),
    )
    original = ExtractCompanyEvents((first, progress), AT + timedelta(days=1), CompanyEventPolicy())
    pending = ExtractCompanyEvents(
        (replace(first, checked=False, text=""), progress),
        AT + timedelta(days=2),
        CompanyEventPolicy(),
        previous_positives=original.positives,
    )
    assert pending.positives[0].positive_level == 0
    corrected = ExtractCompanyEvents(
        (replace(first, text=first.text.replace("用于注销", "用于员工持股")), progress),
        AT + timedelta(days=2),
        CompanyEventPolicy(),
        previous_positives=original.positives,
    )
    assert corrected.positives[0].positive_level == 0


def test_reduction_deadline_and_completion_tail_do_not_extend_on_repeated_closure():
    first = _document(
        "reduce", "控股股东减持计划公告", "本公司控股股东拟减持，减持期间为2026年10月28日至2026年11月28日。"
    )
    events = ExtractCompanyEvents((first,), AT, CompanyEventPolicy())
    assert events.risks[0].reduction_plan.planned_end_date.isoformat() == "2026-11-28"
    close_at = AT + timedelta(days=5)
    closure = _document("end", "控股股东提前终止减持公告", "本公司控股股东根据公告reduce，提前终止减持。", close_at)
    resolved = ExtractCompanyEvents((closure,), close_at, CompanyEventPolicy(), previous_risks=events.risks)
    assert CompanyRiskActive(resolved.risks[0], close_at + timedelta(days=89))
    assert not CompanyRiskActive(resolved.risks[0], close_at + timedelta(days=90))
    repeated = ExtractCompanyEvents(
        (replace(closure, disclosure=replace(closure.disclosure, published_at=close_at + timedelta(days=10))),),
        close_at + timedelta(days=10),
        CompanyEventPolicy(),
        previous_risks=resolved.risks,
    )
    assert repeated.risks[0].expires_at == resolved.risks[0].expires_at


def test_compact_publication_roundtrips_all_positive_measurements_and_rejects_inconsistent_risk():
    facts = _facts(
        (
            _document("warning", "关于收到警示函的公告", "本公司收到证监会出具警示函的决定。"),
            _document("buy", "关于回购股份的公告", "本公司已实际回购股份，占公司总股本的比例为0.5%，用于注销。"),
            _document("increase", "控股股东增持公告", "本公司控股股东已实际增持股份，占公司总股本的比例为0.3%。"),
            _document(
                "contract",
                "关于签订合同的公告",
                "本公司已签订正式合同，合同已生效。合同金额人民币2亿元（不含税）；2025年度经审计的营业收入人民币10亿元。",
            ),
        )
    )
    row = dict(zip(PUBLICATION_COLUMNS, EncodeCompanyPublication(facts), strict=True))
    restored = DecodeCompanyPublication(row)
    assert restored == facts
    assert {item.event_type.value for item in restored.positive_events} == {
        "share_repurchase",
        "shareholder_increase",
        "operating_contract",
    }
    assert {item.positive_level for item in restored.positive_events} == {2, 3}
    assert "payload" not in row and "version" not in row and "body" not in row
    risk = json.loads(row["risk_events_json"])
    risk[0]["risk_level"] = 4
    with pytest.raises(ValueError, match="severity_mismatch"):
        DecodeCompanyPublication({**row, "risk_events_json": json.dumps(risk)})
