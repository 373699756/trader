"""Money-independent score arithmetic, event verification and temporal boundaries."""

from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.download.domain.company_descriptions import CompanyPositiveDescription, CompanyRiskDescription
from trader.download.domain.company_event_extraction import ExtractCompanyEvents
from trader.download.domain.company_events import CompanyDataState, CompanyEventPolicy, CompanySubject
from trader.download.domain.company_evidence import CompanyDocument, CompanyFinancial, CompanyRatio, Disclosure
from trader.download.domain.company_facts import CompanyFacts
from trader.download.infra.company_facts_index import SQLiteCompanyFactsIndex
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction
from trader.recommendation.domain.evidence.review import RiskFact
from trader.recommendation.domain.publication.score_ledger import ScoreAdjustmentItem, ScoreLedgerPayload
from trader.recommendation.domain.scoring.company import (
    AssessCompanyAdjustments,
    BuildCompanyScoringContext,
    BuildScoreLedger,
    CompanyAdjustments,
    CompanyFinancialScores,
    CompanyScoringPolicy,
    IsCompanyRiskEffective,
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
    assert [event.risk_level for event in result.risks if IsCompanyRiskEffective(event, later)] == [4]
    closure = _document("closure", "关于调查终结的公告", "本公司收到证监会〔2026〕调查1号通知，调查终结。", later)
    resolved = ExtractCompanyEvents((closure,), later, CompanyEventPolicy(), previous_risks=result.risks)
    assert not any(IsCompanyRiskEffective(event, later) for event in resolved.risks)


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
def test_unknown_coverage_and_effective_risk_and_disabled_candidate_never_grant_rewards(mode):
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
    assert IsCompanyRiskEffective(resolved.risks[0], close_at + timedelta(days=89))
    assert not IsCompanyRiskEffective(resolved.risks[0], close_at + timedelta(days=90))
    repeated = ExtractCompanyEvents(
        (replace(closure, disclosure=replace(closure.disclosure, published_at=close_at + timedelta(days=10))),),
        close_at + timedelta(days=10),
        CompanyEventPolicy(),
        previous_risks=resolved.risks,
    )
    assert repeated.risks[0].expires_at == resolved.risks[0].expires_at


def test_compact_publication_roundtrips_all_positive_measurements_and_rejects_inconsistent_risk(tmp_path):
    facts = _facts(
        (
            _document("warning", "关于收到警示函的公告", "本公司收到证监会出具警示函的决定。"),
            _document("buy", "关于回购股份的公告", "本公司已实际回购股份，占公司总股本的比例为0.5%，用于注销。"),
            _document("increase", "控股股东增持公告", "本公司控股股东已实际增持股份，占公司总股本的比例为0.3%。"),
            _document(
                "contract",
                "关于签订合同的公告",
                "本公司已签订正式合同，合同已生效。合同金额人民币2亿元（不含税）；"
                "2025年度经审计的营业收入人民币10亿元（不含税）。2025年年度报告于2026年4月20日披露。",
            ),
        )
    )
    index = SQLiteCompanyFactsIndex(tmp_path / "company_facts.sqlite3")
    index.initialize()
    sources = {key: "cninfo" for event in (*facts.risk_events, *facts.positive_events) for key in event.evidence_ids}
    index.publish((facts,), sources, None, AT)
    reader = SQLiteCompanyEvidenceReader(index.path)
    restored = reader.read((facts.code,))[0]
    assert restored == replace(
        facts,
        risk_events=tuple(replace(item, summary=CompanyRiskDescription(item.risk_type)) for item in facts.risk_events),
        positive_events=tuple(
            replace(item, summary=CompanyPositiveDescription(item.event_type)) for item in facts.positive_events
        ),
    )
    assert {item.event_type.value for item in restored.positive_events} == {
        "share_repurchase",
        "shareholder_increase",
        "operating_contract",
    }
    assert {item.positive_level for item in restored.positive_events} == {2, 3}
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        columns = {row[1] for row in connection.run("PRAGMA table_info(company_facts)")}
        assert not columns.intersection({"payload", "body", "risk_events_json", "positive_events_json"})
        connection.run("PRAGMA ignore_check_constraints=ON")
        connection.run("UPDATE company_risk_event SET level=4 WHERE risk_type<=3")
    with pytest.raises(ValueError, match="type_level_mismatch"):
        reader.read((facts.code,))


def test_other_subject_negation_does_not_erase_controlling_shareholder_investigation():
    document = _document(
        "holder",
        "关于控股股东收到立案告知书的公告",
        "控股股东收到中国证券监督管理委员会立案告知书〔2026〕调查1号，决定立案。本公司未被立案。",
    )
    facts = _facts((document,))
    assert len(facts.risk_events) == 1
    event = facts.risk_events[0]
    assert event.risk_level == 4 and event.subject_type is CompanySubject.CONTROLLING_SHAREHOLDER
    assert event.case_id == "〔2026〕调查1号" and IsCompanyRiskEffective(event, AT)
    assert _assess(facts, enabled=False).risk_level == 4


def test_generic_title_with_mixed_subjects_cannot_turn_issuer_denial_into_clear_coverage():
    document = _document(
        "mixed",
        "关于收到立案告知书的公告",
        "控股股东收到证监会立案告知书〔2026〕调查1号。本公司未被立案。",
    )
    facts = _facts((document,))
    assert not facts.risk_events
    assert any(item.reason_code == "subject_unverified" for item in facts.review_reasons)


@pytest.mark.parametrize(
    "body",
    (
        "本公司收到证监会立案告知书〔2026〕调查1号。关于〔2026〕调查2号，本公司未被立案。",
        "本公司收到证监会立案告知书〔2026〕调查1号及〔2026〕调查2号。",
    ),
)
def test_conflicting_cases_of_same_subject_stay_unknown_and_cannot_clear_previous_risk(body):
    original = _document("case", "收到立案告知书", "本公司收到证监会立案告知书〔2026〕调查1号。")
    previous = ExtractCompanyEvents((original,), AT, CompanyEventPolicy())
    ambiguous = _document("ambiguous", "收到立案告知书", body)
    result = ExtractCompanyEvents((ambiguous,), AT, CompanyEventPolicy(), previous_risks=previous.risks)
    assert result.risks == previous.risks
    assert any(item.reason_code == "case_conclusion_ambiguous" for item in result.reviews)
    negative = _document("negative", "关于立案事项的说明", "本公司未被立案，不涉及立案。")
    assert not ExtractCompanyEvents((negative,), AT, CompanyEventPolicy()).risks


def test_another_subject_cannot_resolve_an_issuer_case_even_when_case_number_matches():
    initial = _document("case", "收到立案告知书", "本公司收到证监会立案告知书〔2026〕调查1号。")
    previous = ExtractCompanyEvents((initial,), AT, CompanyEventPolicy())
    closure = _document("closure", "关于控股股东调查终结的公告", "控股股东收到证监会〔2026〕调查1号通知，调查终结。")
    result = ExtractCompanyEvents((closure,), AT, CompanyEventPolicy(), previous_risks=previous.risks)
    assert result.risks == previous.risks and result.reviews[0].reason_code == "resolution_unlinked"


def test_same_case_number_cannot_merge_events_of_different_subjects():
    issuer = _document("issuer", "收到立案告知书", "本公司收到证监会立案告知书〔2026〕调查1号。")
    holder = _document("holder", "控股股东收到立案告知书", "控股股东收到证监会立案告知书〔2026〕调查1号。")
    result = ExtractCompanyEvents((issuer, holder), AT, CompanyEventPolicy())
    assert len(result.risks) == 2
    assert {event.subject_type for event in result.risks} == {
        CompanySubject.ISSUER,
        CompanySubject.CONTROLLING_SHAREHOLDER,
    }


def test_case_reference_does_not_resolve_a_different_or_ambiguous_case():
    initial = _document("case", "收到立案告知书", "本公司收到证监会立案告知书〔2026〕调查1号。")
    previous = ExtractCompanyEvents((initial,), AT, CompanyEventPolicy())
    for body in (
        "本公司关于公告case作出说明，证监会〔2026〕调查2号调查终结。",
        "本公司收到证监会〔2026〕调查1号及〔2026〕调查2号通知，调查终结。",
    ):
        result = ExtractCompanyEvents(
            (_document("closure", "关于调查终结的公告", body),), AT, CompanyEventPolicy(), previous_risks=previous.risks
        )
        assert result.risks == previous.risks and result.reviews[0].reason_code == "resolution_unlinked"


@pytest.mark.parametrize(
    ("body", "level", "ratio"),
    (
        ("本公司已回购股份10000股，拟回购股份占公司总股本的比例为0.5%，用于注销。", 0, None),
        ("本公司已回购股份10000股，计划回购股份占公司总股本的比例为0.5%，用于注销。", 0, None),
        ("本公司未累计回购股份占公司总股本的比例为0.5%，用于注销。", 0, None),
        ("本公司已实际回购股份，占公司总股本的比例为0.3%，用于注销。拟回购比例为0.5%。", 2, 0.3),
    ),
)
def test_repurchase_ratio_is_bound_to_actual_action_and_not_a_later_plan(body, level, ratio):
    result = ExtractCompanyEvents((_document("buy", "关于回购股份的公告", body),), AT, CompanyEventPolicy())
    assert result.positives[0].positive_level == level
    assert result.positives[0].measurements.actual_ratio_pct == ratio
    if level == 0:
        assert result.reviews and not result.positives[0].level_milestones


@pytest.mark.parametrize(
    ("contract_basis", "revenue_basis", "disclosure", "level"),
    (
        ("不含税", "不含税", "2025年年度报告于2026年4月20日披露。", 3),
        ("含税", "不含税", "2025年年度报告于2026年4月20日披露。", 0),
        ("不含税", "含税", "2025年年度报告于2026年4月20日披露。", 0),
        ("不含税", "", "2025年年度报告于2026年4月20日披露。", 0),
        ("不含税", "不含税", "", 0),
        ("不含税", "不含税", "2025年年度报告于2026年12月20日披露。", 0),
    ),
)
def test_contract_requires_separate_net_amounts_and_prior_annual_report_disclosure(
    contract_basis, revenue_basis, disclosure, level
):
    body = (
        f"本公司已签订正式合同。合同总金额为人民币2000万元（{contract_basis}）。"
        f"2025年度经审计的营业收入为人民币10000万元（{revenue_basis}）。{disclosure}"
    )
    result = ExtractCompanyEvents((_document("contract", "关于签订重大合同的公告", body),), AT, CompanyEventPolicy())
    event = result.positives[0]
    assert event.positive_level == level
    if level:
        assert event.measurements.amount_to_revenue_pct == 20
        assert event.measurements.reference_disclosed_at.date().isoformat() == "2026-04-20"
    else:
        assert result.reviews and event.measurements.amount_basis == "unknown"
