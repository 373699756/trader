"""Only an explicitly isolated qualifying holder can supply multi-actor measurements."""

import hashlib
from datetime import date

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company.company_qualified_reduction import BODY, DIRECTOR, HOLDER, STATEMENT, QualifiedReductionDocument
from trader.download.domain.company_event_extraction import ExtractCompanyEvents, ExtractCompanyFieldEvidence
from trader.download.domain.company_events import CompanyEventStatus, CompanySubject


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
