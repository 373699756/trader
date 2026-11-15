"""Annual source columns preserve the report year, monetary scale and issuer."""

from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from trader.download.domain.company_annual_report import ExtractCompanyAnnualReportRevenue
from trader.download.domain.company_evidence import CompanyDocument, Disclosure


_AT = datetime(2026, 4, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
_BODY = (
    "证券代码：003008\n许昌开普检测研究院股份有限公司2025年年度报告摘要\n"
    "近三年主要会计数据和财务指标\n单位：元\n"
    "2025年 2024年 本年比上年增减 2023年\n"
    "营业收入 232,728,984.80 221,337,803.42 5.15% 190,825,407.27\n"
)


def _document(body=_BODY):
    return CompanyDocument(Disclosure("cninfo", "annual-report", ("003008",), "2025年年度报告摘要", _AT, "", "record"), body, True)


def test_current_annual_column_cannot_absorb_digits_from_the_comparative():
    # CNInfo 1225089203, page 4, 2025 column; the 2024 amount starts with 221.
    result = ExtractCompanyAnnualReportRevenue(_document())
    assert result.report_date.isoformat() == "2025-12-31" and result.revenue == 232728984.8
    assert result.statement in "".join(_BODY.split()) and "2025年2024年" in result.statement


def test_sse_company_code_and_percentage_header_without_row_percent():
    # SSE 688700_20260428_6FAP.pdf, page 8; percentage appears in the header.
    body = _BODY.replace("证券代码", "公司代码").replace("003008", "688700")
    body = body.replace("单位：元", "单位：元币种：人民币").replace("本年比上年增减", "本期比上年同期增减(%)")
    body = body.replace("232,728,984.80 221,337,803.42 5.15% 190,825,407.27",
                        "1,098,386,778.65 749,997,504.95 46.45 909,229,904.80")
    document = _document(body)
    document = replace(document, disclosure=replace(document.disclosure, codes=("688700",)))
    assert ExtractCompanyAnnualReportRevenue(document).revenue == 1098386778.65


@pytest.mark.parametrize("code", ("003008", "003009", "003008、股票代码003009"))
def test_full_report_uses_the_company_information_section_and_row_unit(code):
    body = _BODY.replace("证券代码：003008\n", "").replace("年度报告摘要", "年度报告")
    body = body.replace("近三年主要会计数据和财务指标", (
        f"第二节公司简介和主要财务指标\n一、公司信息\n股票简称开普检测股票代码{code}\n二、联系人和联系方式\n六、主要会计数据和财务指标"
    )).replace("单位：元\n", "").replace("营业收入 ", "营业收入（元） ")
    document = replace(_document(body), disclosure=replace(_document().disclosure, title="2025年年度报告"))
    parsed = ExtractCompanyAnnualReportRevenue(document)
    assert (parsed.revenue if parsed else None) == (232728984.8 if code == "003008" else None)


@pytest.mark.parametrize(("old", "new"), (
    ("证券代码：003008", "证券代码：003009"),
    ("证券代码：003008", "证券代码：003008、股票代码：003009"),
    ("2025年 2024年", "2024年 2025年"),
    ("单位：元", "单位：万元"),
    ("单位：元", "单位：亿元"),
    ("单位：元", "单位：元币种：美元"),
    ("主要会计数据", "分季度会计数据"),
    ("营业收入", "营业总收入"),
    ("232,728,984.80", "232,728,984.801"),
    ("232,728,984.80 221,337,803.42", "232,728,984.80221,337,803.42"),
    ("232,728,984.80", "-232,728,984.80"),
    ("232,728,984.80", "不适用"),
))
def test_unbound_subject_year_unit_or_malformed_money_is_not_a_verified_source(old, new):
    assert ExtractCompanyAnnualReportRevenue(_document(_BODY.replace(old, new))) is None


@pytest.mark.parametrize("change", ("unofficial", "unchecked", "terminal", "future_period", "revision", "duplicate", "header_year"))
def test_unsupported_or_ambiguous_material_is_rejected(change):
    document = _document()
    if change == "unofficial":
        document = replace(document, disclosure=replace(document.disclosure, source="news"))
    elif change == "unchecked":
        document = replace(document, checked=False)
    elif change == "terminal":
        document = replace(document, terminal_failure=True)
    elif change == "future_period":
        document = replace(document, disclosure=replace(document.disclosure, published_at=_AT.replace(year=2025)))
    elif change == "revision":
        document = replace(document, disclosure=replace(document.disclosure, title="2025年年度报告修订版"))
    elif change == "header_year":
        document = replace(document, text=_BODY.replace("2025年年度报告摘要", "2024年年度报告摘要"))
    else:
        document = replace(document, text=_BODY + _BODY)
    assert ExtractCompanyAnnualReportRevenue(document) is None
