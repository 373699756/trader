from tests.helpers.shared.configuration import SqlitePolicyFixture

from dataclasses import replace

from datetime import timedelta


import pytest

from trader.download.domain.company_descriptions import CompanyPositiveDescription, CompanyRiskDescription

from trader.download.domain.company_event_extraction import ExtractCompanyEvents, ExtractCompanyLinkCandidate

from trader.download.domain.company_events import CompanyDataState, CompanyPositiveStatus, CompanyPositiveType, CompanySubject, RepurchasePurpose



from trader.download.infra.company_facts_index import SQLiteCompanyFactsIndex

from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader

from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction

from trader.recommendation.domain.evidence.review import RiskFact

from trader.recommendation.domain.publication.score_ledger import ScoreLedgerPayload

from trader.recommendation.domain.scoring.company import BuildCompanyScoringContext, CompanyScoringCalendar, BuildScoreLedger, IsCompanyRiskEffective, ScoreLedgerInput

from trader.recommendation.infra.persistence.score_ledger_codec import DecodeScoreLedger


from tests.helpers.download.company.company_score_ledger import (
    AT,
    CompanyEventPolicy,
    CompanyScoringPolicy,
    _adjustment,
    _assess,
    _document,
    _facts,
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


@pytest.mark.parametrize(
    ("title", "status"),
    (
        ("关于回购股份的公告", CompanyPositiveStatus.IMPLEMENTING),
        ("关于完成回购股份的公告", CompanyPositiveStatus.COMPLETED),
    ),
)
def test_implemented_employee_buyback_keeps_actual_facts_without_awards(title, status):
    document = _document("employee", title, "本公司累计已回购股份，占公司总股本的比例为0.3%，用于员工持股。")
    result = ExtractCompanyEvents((document,), AT, CompanyEventPolicy())
    event = result.positives[0]
    assert event.status is status
    assert event.measurements.actual_ratio_pct == 0.3
    assert event.measurements.purpose is RepurchasePurpose.EMPLOYEE_PLAN
    assert event.positive_level == 0 and event.level_milestones == () and result.reviews


@pytest.mark.parametrize(
    ("purpose", "expected_purpose", "level"),
    (
        ("用于注销", RepurchasePurpose.CANCELLATION, 2),
        ("注销并减少注册资本", RepurchasePurpose.CANCELLATION, 2),
        ("不用于注销", RepurchasePurpose.UNKNOWN, 0),
        ("不再用于注销", RepurchasePurpose.UNKNOWN, 0),
        ("并非用于注销", RepurchasePurpose.UNKNOWN, 0),
        ("不注销并减少注册资本", RepurchasePurpose.UNKNOWN, 0),
        ("不用于注销，用于员工持股", RepurchasePurpose.EMPLOYEE_PLAN, 0),
    ),
)
def test_repurchase_cancellation_purpose_requires_an_affirmative_statement(purpose, expected_purpose, level):
    document = _document(
        "purpose", "关于回购股份的公告", f"本公司累计已回购股份，占公司总股本的比例为0.3%，{purpose}。"
    )
    event = ExtractCompanyEvents((document,), AT, CompanyEventPolicy()).positives[0]
    assert event.status is CompanyPositiveStatus.IMPLEMENTING
    assert event.measurements.actual_ratio_pct == 0.3
    assert event.measurements.purpose is expected_purpose
    assert event.positive_level == level
    if level == 0:
        assert event.level_milestones == ()


def test_positive_progress_cannot_replace_another_subjects_event():
    first = _document(
        "holder", "控股股东增持公告", "控股股东累计已增持股份，占公司总股本的比例为0.3%。"
    )
    previous = ExtractCompanyEvents((first,), AT, CompanyEventPolicy())
    at = AT + timedelta(days=1)
    progress = _document(
        "controller-progress", "实际控制人增持进展公告",
        "实际控制人根据公告holder，累计已增持股份，占公司总股本的比例为0.5%。", at,
    )
    result = ExtractCompanyEvents((progress,), at, CompanyEventPolicy(), previous_positives=previous.positives)
    assert result.positives == previous.positives
    assert result.reviews[0].reason_code == "progress_unlinked"


def test_latest_verified_measurement_retires_only_same_event_gap_even_when_first_sync_is_late():
    plan = _document("plan", "关于回购股份的公告", "本公司拟回购股份，股份用于注销。")
    other = _document("other", "关于回购股份的公告", "本公司拟回购股份，股份用于注销。")
    later = AT + timedelta(days=3)
    previous = ExtractCompanyEvents((plan, other), later, CompanyEventPolicy())
    progress = _document(
        "progress", "关于回购股份进展的公告",
        "本公司根据公告plan，累计已回购股份占公司总股本的比例为0.3%，股份用于注销。", AT + timedelta(days=1),
    )
    result = ExtractCompanyEvents(
        (progress,), later, CompanyEventPolicy(), previous_positives=previous.positives,
        previous_reviews=previous.reviews,
    )
    event = next(event for event in result.positives if "plan" in event.evidence_ids)
    assert event.measurements.actual_ratio_pct == 0.3 and event.positive_level == 2
    assert not any(review.evidence_id == "plan" for review in result.reviews)
    assert any(review.evidence_id == "other" for review in result.reviews)
    restored = ExtractCompanyEvents(
        (), later, CompanyEventPolicy(), previous_positives=result.positives, previous_reviews=result.reviews,
    )
    assert restored.positives == result.positives


def test_verified_progress_cannot_clear_pending_original_source_revision():
    plan = _document("plan", "关于回购股份的公告", "本公司拟回购股份，股份用于注销。")
    previous = ExtractCompanyEvents((plan,), AT, CompanyEventPolicy())
    later = AT + timedelta(days=2)
    progress = _document(
        "progress", "关于回购股份进展的公告",
        "本公司根据公告plan，累计已回购股份占公司总股本的比例为0.3%，股份用于注销。", later,
    )
    pending = replace(plan, checked=False, text="")
    result = ExtractCompanyEvents(
        (pending, progress), later, CompanyEventPolicy(), previous_positives=previous.positives,
        previous_reviews=previous.reviews,
    )
    assert any(review.reason_code == "document_pending" for review in result.reviews)
    assert result.positives[0].positive_level == 0
    assert result.positives[0].not_eligible_reason == "revision_pending"


def test_positive_source_revision_with_another_subject_keeps_one_reviewed_event():
    first = _document(
        "holder", "控股股东增持公告", "控股股东累计已增持股份，占公司总股本的比例为0.3%。"
    )
    previous = ExtractCompanyEvents((first,), AT, CompanyEventPolicy())
    revised = replace(
        first,
        disclosure=replace(first.disclosure, title="实际控制人增持公告", digest="changed"),
        text="实际控制人累计已增持股份，占公司总股本的比例为0.5%。",
    )
    result = ExtractCompanyEvents(
        (revised,), AT + timedelta(days=1), CompanyEventPolicy(), previous_positives=previous.positives
    )
    assert len(result.positives) == 1
    event = result.positives[0]
    assert event.event_id == previous.positives[0].event_id
    assert event.subject_type is CompanySubject.CONTROLLING_SHAREHOLDER
    assert event.measurements == previous.positives[0].measurements and event.positive_level == 0
    assert result.reviews[0].reason_code == "subject_unverified"


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
        CompanyScoringCalendar(tuple((AT + timedelta(days=i)).date() for i in range(-5, 40) if (AT + timedelta(days=i)).weekday() < 5)),
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
        CompanyScoringCalendar(tuple((AT + timedelta(days=i)).date() for i in range(200, 240) if (AT + timedelta(days=i)).weekday() < 5)),
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


@pytest.mark.parametrize(
    ("body", "ratio"),
    (
        ("本公司控股股东拟减持不超过100万股，占公司总股本的比例不超过2%。", 2),
        ("本公司控股股东计划减持股份，占公司总股本的1.5%。", 1.5),
        ("本公司控股股东拟通过集中竞价减持股份，占总股本比例为0.3%。", 0.3),
        ("本公司控股股东拟减持股份，占公司流通股本的比例为2%。", None),
        ("本公司控股股东拟减持股份，占其持有股份的比例为2%。", None),
        ("本公司控股股东拟减持股份，其持有股份占公司总股本的比例为2%。", None),
        ("本公司控股股东拟减持股份，占公司总股本的比例为101%。", None),
        ("本公司控股股东拟减持股份，占公司总股本的比例为2%；拟减持股份占总股本比例为1%。", None),
        ("本公司控股股东张三及李四拟减持股份，占公司总股本的比例为2%。", None),
        ("本公司控股股东张三、李四拟减持股份，占公司总股本的比例为2%。", None),
        ("本公司控股股东与一致行动人拟减持股份，合计占公司总股本的比例为2%。", None),
        ("本公司一、股东基本情况。二、减持计划。本公司控股股东拟减持股份，占公司总股本的比例为2%。", 2),
        ("本公司报告期内合同合计10项。本公司控股股东拟减持股份，占公司总股本的比例为2%。", 2),
        ("一、本公司控股股东拟减持股份，占公司总股本的比例为2%。二、其它说明。", 2),
    ),
)
def test_reduction_plan_extracts_only_unique_planned_total_share_ratio(body, ratio):
    result = ExtractCompanyEvents(
        (_document("reduce", "控股股东减持计划公告", body),), AT, CompanyEventPolicy()
    )
    assert result.risks[0].reduction_plan.planned_ratio_pct == ratio


def test_reduction_new_partial_disclosure_preserves_known_plan_fields_and_ignores_late_old_input():
    initial = _document(
        "reduce", "控股股东减持计划公告",
        "本公司控股股东拟减持股份，占公司总股本的比例为2%，减持期间为2026年10月28日至2026年11月28日。",
    )
    previous = ExtractCompanyEvents((initial,), AT, CompanyEventPolicy())
    later = AT + timedelta(days=1)
    update = _document(
        "update", "控股股东减持计划变更公告",
        "本公司控股股东根据公告reduce，减持计划调整，减持期间为2026年11月1日至2026年11月30日。", later,
    )
    current = ExtractCompanyEvents((update,), later, CompanyEventPolicy(), previous_risks=previous.risks)
    plan = current.risks[0].reduction_plan
    assert plan.planned_ratio_pct == 2
    assert plan.planned_end_date.isoformat() == "2026-11-30"
    restored = ExtractCompanyEvents((initial,), later, CompanyEventPolicy(), previous_risks=current.risks)
    assert restored.risks == current.risks


@pytest.mark.parametrize("intent", ("不计划减持", "不拟减持", "尚未计划减持"))
def test_negated_reduction_intent_does_not_create_a_hard_risk(intent):
    document = _document(
        "reduce", "控股股东减持计划公告", f"本公司控股股东{intent}股份，占公司总股本的比例为2%。"
    )
    assert not ExtractCompanyEvents((document,), AT, CompanyEventPolicy()).risks


def test_compact_publication_roundtrips_all_positive_measurements_and_rejects_inconsistent_risk(tmp_path):
    facts = _facts(
        (
            _document("warning", "关于收到警示函的公告", "本公司收到证监会出具警示函的决定。"),
            _document("reduction", "控股股东减持计划公告", "本公司控股股东拟减持股份，占公司总股本的比例为2%。"),
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
    event_policy = CompanyEventPolicy()
    index = SQLiteCompanyFactsIndex(tmp_path / "company_facts.sqlite3", event_policy=event_policy, sqlite_policy=SqlitePolicyFixture())
    index.initialize()
    sources = {key: "cninfo" for event in (*facts.risk_events, *facts.positive_events) for key in event.evidence_ids}
    index.publish((facts,), sources, None, AT)
    reader = SQLiteCompanyEvidenceReader(index.path, event_policy=event_policy, sqlite_policy=SqlitePolicyFixture())
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
    assert next(item for item in restored.risk_events if item.reduction_plan is not None).reduction_plan.planned_ratio_pct == 2
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        columns = {row[1] for row in connection.run("PRAGMA table_info(company_facts)")}
        assert not columns.intersection({"payload", "body", "risk_events_json", "positive_events_json"})
        connection.run("PRAGMA ignore_check_constraints=ON")
        connection.run("UPDATE company_risk_event SET level=4 WHERE risk_type<=3")
    # Exercise the logical decoder after a deliberate, sealed fixture mutation.
    index.publish_file()
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
    assert event.status is CompanyPositiveStatus.IMPLEMENTING
    if level:
        assert event.measurements.amount_to_revenue_pct == 20
        assert event.measurements.reference_disclosed_at.date().isoformat() == "2026-04-20"
    else:
        assert result.reviews and event.measurements.amount_basis == "unknown"


def test_unimplemented_contract_stays_planned_even_with_comparable_amounts():
    body = (
        "本公司拟签订正式合同。合同总金额为人民币2000万元（不含税）。"
        "2025年度经审计的营业收入为人民币10000万元（不含税）。"
        "2025年年度报告于2026年4月20日披露。"
    )
    result = ExtractCompanyEvents(
        (_document("planned-contract", "关于签订重大合同的公告", body),), AT, CompanyEventPolicy()
    )
    event = result.positives[0]
    assert event.status is CompanyPositiveStatus.PLANNED and not event.measurements.binding_confirmed
    assert event.measurements.amount_to_revenue_pct == 20
    assert event.positive_level == 0 and event.level_milestones == () and result.reviews


@pytest.mark.parametrize(
    ("title", "body", "ratio"),
    (
        ("关于回购股份的公告", "本公司累计已回购股份100万股，占总股本比例为0.3%，用于注销。", 0.3),
        ("控股股东增持公告", "本公司控股股东累计已增持股份，占公司总股本的0.3%。", 0.3),
        ("关于回购股份的公告", "本公司累计已回购股份，计划回购股份占总股本比例为0.3%，用于注销。", None),
        ("关于回购股份的公告", "本公司未累计已回购股份，占总股本比例为0.3%，用于注销。", None),
        ("控股股东增持公告", "本公司控股股东累计已增持股份，目前持有股份占总股本比例为8%。", None),
    ),
)
def test_share_action_common_actual_ratio_wording_keeps_plan_and_negation_guards(title, body, ratio):
    event = ExtractCompanyEvents((_document("action", title, body),), AT, CompanyEventPolicy()).positives[0]
    assert event.measurements.actual_ratio_pct == ratio


def test_late_old_positive_input_cannot_replace_newer_measurement_or_cancel_it():
    first = _document("buy", "关于回购股份的公告", "本公司已实际回购股份，占公司总股本的比例为0.3%，用于注销。")
    later = AT + timedelta(days=2)
    progress = _document(
        "progress", "关于回购股份进展的公告",
        "本公司根据公告buy，累计已回购股份，占公司总股本的比例为0.5%，用于注销。", later,
    )
    current = ExtractCompanyEvents((first, progress), later, CompanyEventPolicy())
    cancellation = _document(
        "old-cancellation", "关于取消回购股份的公告", "本公司根据公告buy取消回购股份。", AT + timedelta(days=1)
    )
    restored = ExtractCompanyEvents(
        (first, cancellation), later, CompanyEventPolicy(), previous_positives=current.positives
    )
    assert restored.positives == current.positives


def test_contract_formatted_cny_amounts_preserve_units_and_prior_annual_basis():
    body = (
        "本公司已签订正式合同。合同总金额：人民币2,000万元（不含税）。"
        "2025年度经审计的营业收入：人民币10，000万元（不含税）。"
        "2025年年度报告于2026年4月20日披露。"
    )
    result = ExtractCompanyEvents((_document("contract", "关于签订重大合同的公告", body),), AT, CompanyEventPolicy())
    measurements = result.positives[0].measurements
    assert measurements.contract_amount_cny == 20_000_000
    assert measurements.reference_annual_revenue_cny == 100_000_000
    assert measurements.amount_to_revenue_pct == 20


def test_contract_conflicting_formatted_amounts_are_not_selected_arbitrarily():
    body = (
        "本公司已签订正式合同。合同总金额：人民币2,000万元（不含税）。"
        "合同金额：人民币3,000万元（不含税）。"
        "2025年度经审计的营业收入：人民币10，000万元（不含税）。"
        "2025年年度报告于2026年4月20日披露。"
    )
    event = ExtractCompanyEvents((_document("contract", "关于签订重大合同的公告", body),), AT, CompanyEventPolicy()).positives[0]
    assert event.measurements.contract_amount_cny is None
    assert event.measurements.reference_annual_revenue_cny == 100_000_000
    assert event.measurements.amount_to_revenue_pct is None


@pytest.mark.parametrize("decision", (
    "公司将根据董事会决议支持控股股东进行回购。",
    "公司将根据董事会决议由子公司进行回购。",
    "控股股东将根据董事会决议进行回购。",
))
def test_repurchase_plan_requires_the_issuer_decision_to_identify_issuer(decision):
    result = ExtractCompanyEvents((_document("plan", "关于回购股份的回购报告书",
                                            "本公司董事会保证公告真实。" + decision + "控股股东暂无减持计划。"),),
                                  AT, CompanyEventPolicy())
    assert result.positives == ()
    assert any(review.reason_code == "subject_unverified" for review in result.reviews)


@pytest.mark.parametrize(("options", "purpose", "level"), (
    ("√减少注册资本□用于员工持股计划或股权激励□用于转换公司可转债□为维护公司价值及股东权益",
     RepurchasePurpose.CANCELLATION, 2),
    ("□减少注册资本√用于员工持股计划或股权激励□用于转换公司可转债□为维护公司价值及股东权益",
     RepurchasePurpose.EMPLOYEE_PLAN, 0),
    ("□减少注册资本□用于员工持股计划或股权激励√用于转换公司可转债□为维护公司价值及股东权益",
     RepurchasePurpose.OTHER, 0),
    ("□减少注册资本□用于员工持股计划或股权激励□用于转换公司可转债√为维护公司价值及股东权益",
     RepurchasePurpose.OTHER, 0),
    ("☑减少注册资本☐用于员工持股计划或股权激励", RepurchasePurpose.CANCELLATION, 2),
    ("□减少注册资本□用于员工持股计划或股权激励", RepurchasePurpose.UNKNOWN, 0),
    ("√减少注册资本√用于员工持股计划或股权激励", RepurchasePurpose.EMPLOYEE_PLAN, 0),
    ("√减少注册资本√用于转换公司可转债", RepurchasePurpose.OTHER, 0),
    ("√减少注册资本。回购股份不用于注销。", RepurchasePurpose.UNKNOWN, 0),
    ("√减少注册资本。本次回购不减少注册资本。", RepurchasePurpose.UNKNOWN, 0),
))
def test_repurchase_template_ignores_unchecked_options(options, purpose, level):
    body = "本公司累计已回购股份占总股本比例为0.3%。回购用途" + options
    event, = ExtractCompanyEvents((_document("buy", "关于回购股份的公告", body),), AT, CompanyEventPolicy()).positives
    assert event.measurements.actual_ratio_pct == 0.3
    assert event.measurements.purpose is purpose
    assert event.positive_level == level


def test_holder_table_material_cannot_retire_an_event_when_another_event_shares_its_evidence():
    plan = _document("table", "关于回购股份的公告", "本公司拟回购股份。")
    old, = ExtractCompanyEvents((plan,), AT, CompanyEventPolicy()).positives
    measured = replace(old, event_id="other-event", status=CompanyPositiveStatus.IMPLEMENTING,
                       measurements=replace(old.measurements, actual_ratio_pct=0.3, purpose=RepurchasePurpose.EMPLOYEE_PLAN))
    title = "关于回购股份事项前十大股东持股情况的公告"
    table = _document("table", title, title + "。本公司董事会保证披露真实。以下为股东名单。")

    result = ExtractCompanyEvents((table,), AT, CompanyEventPolicy(), previous_positives=(old, measured))

    assert set(result.positives) == {old, measured}


@pytest.mark.parametrize("decision", (
    "公司将在回购期限内根据市场情况择机作出回购决策予以实施。",
    "公司将根据董事会决议，在回购期限内，根据市场情况择机进行回购。",
))
def test_issuer_repurchase_plan_can_include_holder_trading_disclosures(decision):
    body = ("本公司董事会保证公告真实。" + decision
            + "公司控股股东、实际控制人暂无减持计划。控股股东累计已增持股份占总股本比例为2%。")
    result = ExtractCompanyEvents((_document("plan", "关于回购股份的回购报告书", body),), AT, CompanyEventPolicy())
    event, = result.positives
    assert event.subject_type is CompanySubject.ISSUER
    assert event.event_type is CompanyPositiveType.SHARE_REPURCHASE
    assert event.status is CompanyPositiveStatus.PLANNED and event.positive_level == 0
    assert event.measurements.actual_ratio_pct is None


@pytest.mark.parametrize(("statement", "ratio", "implemented"), (
    ("本公司首次回购股份987791股，占公司总股本的比例约为0.10%。", 0.1, True),
    ("本公司以集中竞价方式首次回购股份100万股，占公司总股本的0.062%。", 0.062, True),
    ("本公司根据相关规定，现将公司首次回购股份情况公告如下：2026年11月5日，公司通过交易系统"
     "首次回购公司股份987,791股，占公司总股本的比例约为0.10%。", 0.1, True),
    ("本公司拟首次回购股份，占公司总股本的比例为0.3%。", None, False),
    ("本公司计划于明日首次回购股份，占公司总股本的比例为0.3%。", None, False),
    ("本公司未首次回购股份，占公司总股本的比例为0.3%。", None, False),
    ("本公司尚未实际首次回购股份，占公司总股本的比例为0.3%。", None, False),
    ("本公司首次回购股份100万股，拟回购股份占公司总股本的比例为0.3%。", None, True),
    ("本公司首次回购股份占公司总股本比例为0.1%；首次回购股份占公司总股本比例为0.2%。", None, True),
    ("本公司首次回购股份占公司总股本比例为150%。", None, False),
    ("本公司首次通过股份回购专用证券账户以集中竞价方式回购公司股份2,580,000股，占总股本0.66%。", None, True),
    ("本公司首次通过股票回购专用证券账户以集中竞价方式回购股份1,295,000股，占总股本0.13%。", None, True),
    ("本公司首次通过股份回购专用证券账户拟回购公司股份100万股。", None, False),
    ("本公司首次通过股份回购专用证券账户完成开户，拟于次日实施。", None, False),
    ("本公司首次—1—回购股份100万股。", None, False),
))
def test_first_repurchase_ratio_is_an_actual_measurement_with_plan_guards(statement, ratio, implemented):
    document = _document("first", "关于首次回购公司股份的公告", statement + "回购股份用于员工持股计划。")
    event, = ExtractCompanyEvents((document,), AT, CompanyEventPolicy()).positives
    assert event.measurements.actual_ratio_pct == ratio
    assert event.measurements.purpose is RepurchasePurpose.EMPLOYEE_PLAN
    assert event.positive_level == 0
    assert event.status is (CompanyPositiveStatus.IMPLEMENTING if implemented else CompanyPositiveStatus.PLANNED)


@pytest.mark.parametrize(("statement", "status"), (
    ("本公司已通过集中竞价方式回购股份，实际比例尚待核实。", CompanyPositiveStatus.IMPLEMENTING),
    ("本公司本次回购计划已实施完成，实际比例尚待核实。", CompanyPositiveStatus.COMPLETED),
    ("本公司已完成股份回购，实际比例尚待核实。", CompanyPositiveStatus.COMPLETED),
    ("本公司累计已回购股份100万股，本次回购计划尚未实施完毕。", CompanyPositiveStatus.IMPLEMENTING),
    ("本公司尚未通过集中竞价方式回购股份。", CompanyPositiveStatus.PLANNED),
    ("本公司拟回购股份。本次回购计划实施完成后，将披露结果。", CompanyPositiveStatus.PLANNED),
    ("本公司本次回购计划已实施完成后，将披露结果。", CompanyPositiveStatus.PLANNED),
    ("本公司已完成回购方案审议，拟于明日首次回购股份。", CompanyPositiveStatus.PLANNED),
    ("本公司上一期回购计划已实施完成，本次拟回购股份。", CompanyPositiveStatus.PLANNED),
    ("若本公司已回购股份100万股，则需披露结果。", CompanyPositiveStatus.PLANNED),
    ("本公司累计已回购股份数量为：0股，尚未开展本次回购。", CompanyPositiveStatus.PLANNED),
    ("本公司累计已回购股份数量为零股。", CompanyPositiveStatus.PLANNED),
    ("本公司累计已回购股份，实际尚未实施。", CompanyPositiveStatus.PLANNED),
    ("本公司拟回购股份。累计回购股份数量（股）：暂缺。", CompanyPositiveStatus.PLANNED),
    ("本公司拟回购股份。同行公司已回购股份100万股。", CompanyPositiveStatus.PLANNED),
    ("本公司拟回购股份。子公司已回购股份100万股。", CompanyPositiveStatus.PLANNED),
    ("本公司拟回购股份。若满足全部实施条件，本公司已回购股份的数量将另行公告。", CompanyPositiveStatus.PLANNED),
    ("本公司已完成本次股份回购的审批。拟下月实施。", CompanyPositiveStatus.PLANNED),
    ("本公司拟开展本轮回购。去年，本公司已回购股份100万股。", CompanyPositiveStatus.PLANNED),
    ("本公司已回购股份100万股，其他公司本次回购计划已实施完成。", CompanyPositiveStatus.IMPLEMENTING),
    ("本公司已回购股份100万股，其他公司宣布，本次回购计划已实施完成。", CompanyPositiveStatus.IMPLEMENTING),
    ("其他公司宣布，本次回购计划已实施完成，本公司已回购股份100万股。", CompanyPositiveStatus.IMPLEMENTING),
))
def test_share_action_status_requires_own_execution_evidence_without_inventing_a_ratio(statement, status):
    document = _document("missing-ratio", "关于回购股份的公告", statement + "回购股份用于注销。")
    result = ExtractCompanyEvents((document,), AT, CompanyEventPolicy())
    event, = result.positives
    assert event.status is status
    assert event.measurements.actual_ratio_pct is None
    assert event.positive_level == 0 and event.level_milestones == ()
    assert event.not_eligible_reason is not None
    assert result.reviews
    for strategy in ("tomorrow", "d25"):
        adjustments = _assess(_facts((document,)), strategy)
        positive = tuple(item for item in adjustments.bonuses if item.kind == "positive_bonus")
        assert positive and all(item.rule_points == 0 and item.reason != "eligible" for item in positive)


@pytest.mark.parametrize(("title", "declaration", "action", "status"), (
    ("控股股东增持股份公告", "本公司收到公司控股股东甲方控股有限公司（以下简称“甲方控股”）的增持意向函。",
     "甲方控股通过交易系统，以集中竞价方式累计增持本公司股份255.67万股，占总股本0.26%。本次增持计划尚未实施完毕。",
     CompanyPositiveStatus.IMPLEMENTING),
    ("控股股东增持股份公告", "证券代码：600001\n发行人股份有限公司\n一、实施进展发行人股份有限公司（以下简称“公司”）于2026年9月29日收到公司控股股东甲方控股有限公司（以下简称“甲方控股”）的增持意向函。",
     "甲方控股通过交易系统，以集中竞价方式累计\n—1—\n增持本公司股份255.67万股，占总股本0.26%。",
     CompanyPositiveStatus.IMPLEMENTING),
    ("控股股东增持股份公告", "本公司控股股东拟增持。其他公司控股股东甲方控股有限公司（以下简称“甲方控股”）公告。",
     "甲方控股通过交易系统，以集中竞价方式累计增持公司股份100万股。", CompanyPositiveStatus.PLANNED),
    ("控股股东增持股份公告", "本公司控股股东甲方控股有限公司（以下简称“甲方控股”）公告。本公司控股股东乙方控股有限公司（以下简称“乙方控股”）公告。",
     "甲方控股通过交易系统，以集中竞价方式累计增持公司股份100万股。", CompanyPositiveStatus.PLANNED),
    ("控股股东增持股份公告", "本公司控股股东甲方控股有限公司（以下简称“甲方控股”）公告。",
     "甲方控股拟通过交易系统，以集中竞价方式累计增持公司股份100万股。", CompanyPositiveStatus.PLANNED),
    ("控股股东增持股份公告", "证券代码：600001\n发行人股份有限公司\n本公司控股股东拟增持。其他股份有限公司（以下简称“公司”）于2026年9月29日收到公司控股股东甲方控股有限公司（以下简称“甲方控股”）的函。",
     "甲方控股通过交易系统，以集中竞价方式累计增持本公司股份100万股。", CompanyPositiveStatus.PLANNED),
    ("实际控制人增持计划实施完成公告", "公司实际控制人、董事长兼总经理甲乙丙女士计划增持公司H股股份。",
     "甲乙丙女士通过集中竞价方式增持公司H股股份36,000,360股，占公司目前总股本0.44%，合计增持金额约人民币100万元，本次增持计划已实施完毕。",
     CompanyPositiveStatus.COMPLETED),
    ("实际控制人增持计划实施完成公告", "公司实际控制人、董事长兼总经理甲乙丙女士计划增持公司H股股份。",
     "其他姓名女士通过集中竞价方式增持公司H股股份100万股，本次增持计划已实施完毕。", CompanyPositiveStatus.PLANNED),
    ("实际控制人增持计划实施完成公告", "公司实际控制人、董事长兼总经理甲乙丙女士计划增持公司H股股份。",
     "甲乙丙女士拟通过集中竞价方式增持公司H股股份100万股，本次增持计划实施完毕后将公告。", CompanyPositiveStatus.PLANNED),
    ("实际控制人增持计划实施完成公告", "公司实际控制人、董事长兼总经理甲乙丙女士计划增持公司H股股份。",
     "甲乙丙女士通过集中竞价方式增持公司H股股份100万股。其他公司宣布新的增持计划。本次增持计划已实施完毕。", CompanyPositiveStatus.IMPLEMENTING),
))
def test_only_explicit_unique_subject_bindings_support_alias_execution_status(title, declaration, action, status):
    document = _document("alias-action", title, declaration + action)
    candidate = ExtractCompanyLinkCandidate(document, AT, CompanyEventPolicy())
    assert candidate is not None and candidate.positive is not None
    event = candidate.positive
    assert event.status is status
    assert event.measurements.actual_ratio_pct is None
    assert event.positive_level == 0 and event.level_milestones == () and event.not_eligible_reason is not None


def test_current_unknown_ratio_cannot_score_historical_milestones():
    first = _document("base-action", "关于回购股份的公告", "本公司已实际回购股份，占公司总股本比例为0.3%，用于注销。")
    old = ExtractCompanyEvents((first,), AT, CompanyEventPolicy())
    at = AT + timedelta(days=1)
    event = replace(
        old.positives[0], status=CompanyPositiveStatus.IMPLEMENTING, positive_level=0,
        measurements=replace(old.positives[0].measurements, actual_ratio_pct=None),
        not_eligible_reason="revision_pending",
    )
    assert event.level_milestones == old.positives[0].level_milestones
    facts = replace(_facts(at=at), positive_events=(event,))
    for strategy in ("tomorrow", "d25"):
        positive = tuple(item for item in _assess(facts, strategy, at=at).bonuses if item.kind == "positive_bonus")
        assert positive and all(item.rule_points == 0 and item.reason != "eligible" for item in positive)
