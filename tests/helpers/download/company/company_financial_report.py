"""Reduced quarterly layout with distinct quarterly and cumulative source cells."""

from datetime import datetime

from tests.helpers.download.company import company_evidence as fixtures
from trader.download.domain.company_evidence import CompanyDocument

BODY = (
    "证券代码：600001\n示例股份有限公司2026年第三季度报告\n"
    "（一）主要会计数据和财务指标\n单位：元 币种：人民币\n"
    "              本报告期      本报告期比上年同期增减      年初至报告期末      年初至报告期末比上年同期增减\n"
    "营业收入（元）  100.00  11.00%  300.00  21.00%\n"
    "归属于上市公司股东的扣除非经常性损益的净利润（元）  1.00  12.00%  2.00  34.00%\n"
    "基本每股收益（元/股）  0.1000  13.00%  0.0000  -100.00%\n"
    "加权平均净资产收益率  2.00%  增加 1.00 个百分点  -0.00%  减少 2.00 个百分点\n"
    "              本报告期末      上年度末\n"
    "总资产（元）  999.00  888.00  12.00%\n"
    "（二）非经常性损益项目和金额\n"
    "合并利润表\n2026年1—9月\n单位：元 币种：人民币\n2026年前三季度  2025年前三季度\n"
    "一、营业总收入  300.00  247.93\n其中：营业收入  300.00  247.93\n二、营业总成本\n"
)
AT = datetime(2026, 10, 29, tzinfo=fixtures.NOW.tzinfo)
WRAPPED_ROE = (
    "加权平均净资产收益率                           减少 1.87 个                               减少 1.35 个\n"
    "                            5.21                                    11.66\n"
    "（%）                                     百分点                                      百分点"
)
WRAPPED_ROE_ALTERNATE = (
    "加权平均净资产收益率                            增加 2.47 个                         增加 10.54 个\n"
    "                               4.85                            16.41\n"
    "（%）                                      百分点                                百分点"
)


def ReportDocument(text=BODY, *, title="2026年第三季度报告"):
    return CompanyDocument(fixtures._disclosure("quarter-report", title, when=AT), text, True)


def WrappedRoeDocument(frame=WRAPPED_ROE):
    row = "加权平均净资产收益率  2.00%  增加 1.00 个百分点  -0.00%  减少 2.00 个百分点"
    return ReportDocument(BODY.replace(row, frame))
