"""Quarterly evidence must bind cumulative columns, units and explicit nonnumeric notes."""

from dataclasses import replace
from datetime import date

import pytest

from tests.helpers.download.company.company_financial_report import BODY, AT, ReportDocument
from trader.download.domain.company_financial_report import ExtractCompanyFinancialReport, CompanyFinancialReportRequest


def test_four_cells_choose_cumulative_eps_and_roe_and_cumulative_growth_without_recalculation():
    parsed = ExtractCompanyFinancialReport(ReportDocument())
    assert parsed.report_date == date(2026, 9, 30)
    assert {cell.field: cell.value for cell in parsed.cells} == {
        "basic_eps": 0.0, "roe_pct": 0.0, "core_profit_growth_pct": 34.0, "revenue_growth_pct": 21.0,
    }
    assert next(cell for cell in parsed.cells if cell.field == "roe_pct").raw_value == "-0.00%"
    assert all(cell.state == "numeric" and cell.statement in BODY for cell in parsed.cells)


@pytest.mark.parametrize("old,new", (
    ("证券代码：600001", "证券代码：600002"),
    ("证券代码：600001", "证券代码：600001\n证券代码：600002"),
    ("2026年第三季度报告", "2025年第三季度报告"),
    ("单位：元 币种：人民币", "单位：万元 币种：人民币"),
    ("单位：元 币种：人民币", "单位：元 币种：美元"),
    ("              本报告期      本报告期比上年同期增减      年初至报告期末", "              年初至报告期末      本报告期比上年同期增减      本报告期"),
    ("（二）非经常性损益项目和金额", "没有表格结束边界"),
))
def test_wrong_identity_period_units_or_column_order_cannot_supply_cells(old, new):
    assert ExtractCompanyFinancialReport(ReportDocument(BODY.replace(old, new))) is None


@pytest.mark.parametrize("title", ("2026年半年度报告", "2026年第三季度报告修订版", "2026年第三季度报告更正公告"))
def test_unhandled_periods_and_corrections_do_not_reuse_an_original_report(title):
    assert ExtractCompanyFinancialReport(ReportDocument(title=title)) is None


@pytest.mark.parametrize("raw,state", (("不适用", "not_applicable"), ("--", "unquantified"), ("-", "unquantified")))
def test_nonnumeric_cumulative_eps_never_becomes_zero(raw, state):
    body = BODY.replace("0.0000", raw)
    parsed = ExtractCompanyFinancialReport(ReportDocument(body))
    cell = next(cell for cell in parsed.cells if cell.field == "basic_eps")
    assert cell.value is None and cell.state == state and cell.raw_value == raw


@pytest.mark.parametrize("note,state", (
    ("注：加权平均净资产收益率没有披露数据的原因：按计算规则所得正数不能代表公司的真实经营情况。", "not_disclosed"),
    ("注：由于公司净资产为负数，加权平均净资产收益率为正值，不具备参考性。", "not_meaningful"),
))
def test_explicit_explanation_keeps_printed_positive_roe_from_becoming_usable(note, state):
    body = BODY.replace("-0.00%", "121.93%").replace("（二）非经常性损益项目和金额", note + "\n（二）非经常性损益项目和金额")
    cell = next(cell for cell in ExtractCompanyFinancialReport(ReportDocument(body)).cells if cell.field == "roe_pct")
    assert cell.state == state and cell.value is None and cell.raw_value == "121.93%"
    assert cell.statement in body


def test_cover_page_can_precede_the_unique_security_header_but_native_column_streams_are_unsupported():
    cover = ("示例股份有限公司2026年第三季度报告\n" + "\n" * 30)
    assert ExtractCompanyFinancialReport(ReportDocument(cover + BODY)) is not None
    assert ExtractCompanyFinancialReport(ReportDocument("\n".join(BODY.split()))) is None


def test_report_request_accepts_only_known_annual_or_third_quarter_dates():
    assert CompanyFinancialReportRequest("600001", date(2026, 9, 30), AT).disclosed_at == AT
    with pytest.raises(ValueError):
        CompanyFinancialReportRequest("600001", date(2026, 6, 30), AT)
    with pytest.raises(ValueError):
        CompanyFinancialReportRequest("600001", date(2026, 12, 31), AT)
    assert ExtractCompanyFinancialReport(replace(ReportDocument(), checked=False)) is None


@pytest.mark.parametrize("old,new", (
    ("合并利润表", "母公司利润表"),
    ("2025年前三季度", "2024年前三季度"),
    ("一、营业总收入  300.00  247.93", "一、营业总收入  310.00  247.93"),
    ("一、营业总收入  300.00  247.93", "一、营业总收入  300.00  240.00"),
    ("其中：营业收入  300.00  247.93", "其中：营业收入  310.00  247.93"),
))
def test_operating_revenue_growth_cannot_replace_total_revenue_without_both_comparable_income_columns(old, new):
    parsed = ExtractCompanyFinancialReport(ReportDocument(BODY.replace(old, new)))
    assert {cell.field for cell in parsed.cells} == {"basic_eps", "roe_pct", "core_profit_growth_pct"}


def test_revenue_basis_is_an_actual_independent_statement_and_zero_denominator_remains_not_applicable():
    cell = next(cell for cell in ExtractCompanyFinancialReport(ReportDocument()).cells if cell.field == "revenue_growth_pct")
    assert cell.basis_statement in BODY and "营业总收入" in cell.basis_statement and "2025年" in cell.basis_statement
    body = BODY.replace("300.00", "0.00").replace("247.93", "0.00").replace("21.00%", "不适用")
    cell = next(cell for cell in ExtractCompanyFinancialReport(ReportDocument(body)).cells if cell.field == "revenue_growth_pct")
    assert cell.value is None and cell.state == "not_applicable" and cell.basis_statement in body
