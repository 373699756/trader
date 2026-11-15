"""Only an explicitly isolated qualifying holder can supply multi-actor measurements."""

import hashlib
from dataclasses import replace
from datetime import date

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company.company_qualified_reduction import BODY, DIRECTOR, HOLDER, STATEMENT, QualifiedReductionDocument
from trader.download.domain.company_event_extraction import ExtractCompanyEvents, ExtractCompanyFieldEvidence
from trader.download.domain.company_evidence import CompanyDocument
from trader.download.domain.company_events import CompanyEventStatus, CompanySubject, ReductionPlan
from trader.download.domain.company_reduction_scope import ExtractCompanyQualifiedReduction


def test_numbered_qualified_plan_proves_its_own_dates_and_total_capital_ratio():
    # Source: https://static.cninfo.com.cn/finalpage/2026-09-24/1225583255.PDF
    document = QualifiedReductionDocument()
    event, = ExtractCompanyEvents((document,), fixtures.NOW, fixtures.CompanyEventPolicy()).risks
    assert event.subject_type is CompanySubject.MAJOR_SHAREHOLDER
    assert event.risk_level == 4 and event.status is CompanyEventStatus.ONGOING
    assert event.event_id == "qualified-plan:major_shareholder_reduction" and event.evidence_ids == ("qualified-plan",)
    plan = event.reduction_plan
    assert (plan.planned_start_date, plan.planned_end_date, plan.planned_ratio_pct) == (
        date(2026, 10, 26), date(2027, 1, 25), 1.4708,
    )
    fields = ExtractCompanyFieldEvidence(document, fixtures.NOW, fixtures.CompanyEventPolicy())
    assert {item.field: item.value for item in fields} == {
        "start_date": date(2026, 10, 26), "end_date": date(2027, 1, 25), "planned_pct": 1.4708,
    }
    for field in fields:
        assert field.state == "verified" and field.event_id == event.event_id
        proof, = field.evidence
        assert proof.quote == STATEMENT and proof.quote in document.text
        assert proof.start_line == proof.end_line == 5
        assert proof.body_hash == hashlib.sha256(document.text.encode()).digest()
        assert proof.subject is CompanySubject.MAJOR_SHAREHOLDER


_SINGLE = (
    "证券代码：600001\n示例科技股份有限公司\n关于持股5%以上股东拟减持股份的预披露公告\n"
    "股东保证本次披露真实。\n特别提示：\n"
    "持有示例科技股份有限公司（以下简称“公司”或“本公司”）股份50,000股（占本公司总股本20%）的"
    "5%以上股东张三先生计划在本公告披露之日起十五个交易日后的三个月内"
    "（即2026年10月20日至2027年1月19日）以集中竞价交易或大宗交易方式减持本公司股份"
    "2,500股（占本公司总股本比例1.00%）。其中，以集中竞价交易方式减持不超过1,000股。\n"
    "公司近日收到张三先生的减持计划告知函。\n一、股东的基本情况\n"
    "张三先生不属于公司控股股东，也不属于公司实际控制人的一致行动人。\n"
    "三、减持股东相关承诺及履行情况\n本人在担任发行人高级管理人员期间，每年转让股份不超过本人所持股份的25%。\n"
)


def _single_document(body=_SINGLE):
    item = replace(fixtures._disclosure("single-qualified"), title="关于持股5%以上股东减持股份的预披露公告")
    return CompanyDocument(item, body, True)


def test_single_declared_plan_is_not_lost_to_negative_roles_or_historical_self_commitments():
    document = _single_document()
    event, = ExtractCompanyEvents((document,), fixtures.NOW, fixtures.CompanyEventPolicy()).risks
    assert event.reduction_plan.planned_end_date == date(2027, 1, 19)
    assert event.expires_at.date() == date(2027, 4, 19)
    fields = ExtractCompanyFieldEvidence(document, fixtures.NOW, fixtures.CompanyEventPolicy())
    assert {item.field: item.value for item in fields} == {
        "start_date": date(2026, 10, 20), "end_date": date(2027, 1, 19), "planned_pct": 1.0,
    }
    assert all(item.evidence[0].quote in _SINGLE and "张三先生" in item.evidence[0].quote for item in fields)


@pytest.mark.parametrize("old,new", (
    ("证券代码：600001", "证券代码：600002"),
    ("示例科技股份有限公司\n", "另一公司股份有限公司\n"),
    ("20%", "4.99%"),
    ("5%以上股东张三先生计划", "5%以上股东张三先生不再计划"),
    ("5%以上股东张三先生计划", "5%以上股东张三先生、李四先生计划"),
    ("5%以上股东张三先生计划", "5%以上股东张三先生及一致行动人计划"),
    ("2027年1月19日", "2027年2月31日"),
    ("2,500股", "50,001股"),
    ("比例1.00%", "比例21.00%"),
    ("股东的基本情况", "股东的基本情况。股东李四先生计划自明日起减持股份。"),
    ("股东的基本情况", "股东的基本情况。该股东与其一致行动人共同执行减持计划。"),
    ("股东的基本情况", "股东的基本情况。其他股东分别执行另行减持计划。"),
    ("股东的基本情况", "股东的基本情况。" + _SINGLE.split("先生", 1)[1].split("。", 1)[0].replace("2027年1月19日", "2027年1月20日")),
    ("股份50,000股", "股份数量待核50,000股"),
))
def test_single_summary_cannot_borrow_another_issuer_holder_or_unbound_measurement(old, new):
    document = _single_document(_SINGLE.replace(old, new))
    assert ExtractCompanyQualifiedReduction(document, CompanySubject.MAJOR_SHAREHOLDER) is None


def test_qualified_controlling_holder_requires_the_matching_role():
    document = _single_document(_SINGLE.replace("5%以上股东张三先生", "控股股东张三先生"))
    assert ExtractCompanyQualifiedReduction(document, CompanySubject.MAJOR_SHAREHOLDER) is None
    assert ExtractCompanyQualifiedReduction(document, CompanySubject.CONTROLLING_SHAREHOLDER) is not None


def test_qualified_holder_need_not_be_the_first_numbered_actor():
    body = BODY.replace("1、" + HOLDER + "\n2、" + DIRECTOR, "1、" + DIRECTOR + "\n2、" + HOLDER)
    results = ExtractCompanyFieldEvidence(QualifiedReductionDocument(body), fixtures.NOW, fixtures.CompanyEventPolicy())
    assert {item.field: item.value for item in results}["planned_pct"] == 1.4708
    assert all(item.evidence[0].start_line == 6 for item in results)


@pytest.mark.parametrize(("old", "new"), (
    ("持有公司股份175,000股，（占公司总股本比例0.06%）", "持有股份数量待核实"),
    ("比例0.06%", "比例6%"),
    ("比例0.06%", "比例未知"),
    ("公司董事会秘书", "其他股东"),
    ("公司董事会秘书", "公司董事兼持股5%以上股东"),
    ("以下简称“恒易有业”", "以下简称“另一主体”"),
    ("为公司首次公开发行股票上市前股东，", "曾为公司股东，曾"),
    ("），恒易有业计划", "），恒易有业暂不计划"),
    ("），恒易有业计划", "），若获批准，恒易有业计划"),
    ("2026年10月26日", "2026年11月31日"),
    ("2027年1月25日", "2025年1月25日"),
    ("4,456,020股", "0股"),
    ("的1.4708%", "的101%"),
    ("占公司总股本的1.4708%，", ""),
    ("1、", "2、"),
    ("公司持股5%以上股东海南", "公司持股5%以上股东另一合伙企业、股东海南"),
    ("证券代码：600001", "证券代码：600002"),
    (STATEMENT, STATEMENT + "另计划减持总股本的2%。"),
    ("\n2、" + DIRECTOR, "\n2、" + HOLDER),
))
def test_unknown_conflicting_unqualified_or_unbound_actor_cannot_supply_measurements(old, new):
    body = BODY.replace(old, new)
    document = QualifiedReductionDocument(body)
    assert not ExtractCompanyFieldEvidence(document, fixtures.NOW, fixtures.CompanyEventPolicy())
    event, = ExtractCompanyEvents((document,), fixtures.NOW, fixtures.CompanyEventPolicy()).risks
    assert event.risk_level == 4 and event.status is CompanyEventStatus.ONGOING
    assert event.reduction_plan.planned_ratio_pct is None


def test_repeated_statement_has_no_arbitrarily_selected_source_proof():
    document = QualifiedReductionDocument(BODY + "\n" + STATEMENT)
    assert not ExtractCompanyFieldEvidence(document, fixtures.NOW, fixtures.CompanyEventPolicy())
    event, = ExtractCompanyEvents((document,), fixtures.NOW, fixtures.CompanyEventPolicy()).risks
    assert event.reduction_plan.planned_ratio_pct == 1.4708
    assert event.status is CompanyEventStatus.ONGOING and event.resolved_at is None


_EMPLOYEE = (
    "证券代码：600001\n示例科技股份有限公司\n特别提示：\n"
    "持有示例科技股份有限公司（以下简称“公司”或“本公司”）股份50,000股（占本公司总股本6.82%）的"
    "大股东王丙友计划在自本公告披露之日起15个交易日后的3个月内"
    "（2026年10月23日至2027年1月22日）以集中竞价交易方式或大宗交易方式减持本公司股份"
    "4,300股（占本公司总股本比例1.84%）。"
    "持有本公司股份5,633股（占本公司总股本2.41%）的招商资管员工参与主板战略配售集合资产管理计划"
    "（以下简称“员工战配计划”）计划在自本公告披露之日起15个交易日后的3个月内"
    "（2026年10月23日至2027年1月22日）以集中竞价交易方式或大宗交易方式减持本公司股份"
    "5,633股（占本公司总股本比例2.41%）。公司近日收到减持告知函。"
)
_GROUP = (
    "证券代码：600001\n示例科技股份有限公司\n"
    "控股股东及其一致行动人阳光化学有限公司、宝联投资中心（有限合伙）保证向本公司提供的信息真实。\n"
    "特别提示：公司控股股东阳光化学有限公司（以下简称“阳光化学”）、宝联投资中心（有限合伙）（以下简称“宝联投资”）"
    "拟通过集中竞价合计减持3%。阳光化学、宝联投资将于本减持计划公告之日起15个交易日之后"
    "（即2026年10月26日至2027年1月25日）的三个月内进行。公司近日收到减持计划告知函。"
)


def test_employee_asset_plan_cannot_supply_a_major_holders_ratio():
    document = _single_document(_EMPLOYEE)
    proof = ExtractCompanyQualifiedReduction(document, CompanySubject.MAJOR_SHAREHOLDER)
    assert proof is not None and proof.planned_ratio_pct == 1.84
    assert proof.end_date == date(2027, 1, 22)
    assert "2.41%" not in proof.statement and "王丙友" in proof.statement


@pytest.mark.parametrize("old,new", (
    ("2.41%", "5.01%"), ("6.82%", "4.99%"),
    ("员工参与主板战略配售集合资产管理计划", "其他投资计划"),
    ("2027年1月22日", "2027年2月31日"),
    ("证券代码：600001", "证券代码：600002"),
    ("公司近日收到", "股东李四计划自明日起减持股份。公司近日收到"),
))
def test_employee_plan_requires_complete_qualification_and_bound_participants(old, new):
    assert ExtractCompanyQualifiedReduction(_single_document(_EMPLOYEE.replace(old, new)), CompanySubject.MAJOR_SHAREHOLDER) is None


def test_controller_concert_group_proves_common_dates_without_inventing_an_individual_ratio():
    item = replace(_single_document().disclosure, title="关于控股股东及其一致行动人减持计划的预披露公告")
    document = CompanyDocument(item, _GROUP, True)
    proof = ExtractCompanyQualifiedReduction(document, CompanySubject.CONTROLLING_SHAREHOLDER)
    assert proof is not None and proof.planned_ratio_pct is None
    assert (proof.start_date, proof.end_date) == (date(2026, 10, 26), date(2027, 1, 25))
    assert "阳光化学、宝联投资" in proof.statement
    event, = ExtractCompanyEvents((document,), fixtures.NOW, fixtures.CompanyEventPolicy()).risks
    old = replace(event, reduction_plan=ReductionPlan(date(2026, 10, 26), date(2027, 1, 25), 3.0))
    current, = ExtractCompanyEvents((document,), fixtures.NOW, fixtures.CompanyEventPolicy(), previous_risks=(old,)).risks
    assert current.reduction_plan.planned_ratio_pct is None
    assert current.reduction_plan.planned_end_date == date(2027, 1, 25)
    assert current.expires_at.date() == date(2027, 4, 25)
    assert (current.event_id, current.evidence_ids, current.risk_level) == (old.event_id, old.evidence_ids, old.risk_level)
    fields = ExtractCompanyFieldEvidence(document, fixtures.NOW, fixtures.CompanyEventPolicy())
    assert {field.field for field in fields} == {"start_date", "end_date"}


@pytest.mark.parametrize("old,new", (
    ("以下简称“宝联投资”", "以下简称“公司”"),
    ("公司近日收到", "其他股东另有计划。公司近日收到"),
    ("公司近日收到", "另有2026年10月26日至2027年2月25日期限。公司近日收到"),
    ("证券代码：600001", "证券代码：600002"),
    ("宝联投资将于", "另一个股东将于"),
))
def test_group_period_rejects_conflicting_dates_actors_or_issuer(old, new):
    assert ExtractCompanyQualifiedReduction(_single_document(_GROUP.replace(old, new)), CompanySubject.CONTROLLING_SHAREHOLDER) is None
