"""Synthetic wording fixtures from the recorded gaps, not retained announcement originals."""

import pytest

from tests.helpers.download.company.company_score_ledger import AT, CompanyEventPolicy, _document
from trader.download.domain.company_event_extraction import ExtractCompanyEvents
from trader.download.domain.company_events import CompanyPositiveStatus, ContractMeasurements


@pytest.mark.parametrize(("body", "ratio"), (
    (
        "本公司控股股东广晟集团持有股份占公司总股本的23.78%。"
        "本公司控股股东广晟集团计划以集中竞价方式减持公司股份，占公司总股本的比例不超过0.60%。",
        0.6,
    ),
    ("本公司控股股东兴泰集团计划减持不超过本公司总股本的1%。", 1),
    ("本公司控股股东拟减持不高于公司总股本的0.6%。", 0.6),
    ("本公司控股股东计划减持不超过公司流通股本的1%。", None),
    ("本公司控股股东计划减持不超过其持有股份的1%。", None),
    ("本公司控股股东计划减持股份，其持有股份不超过公司总股本的1%。", None),
    ("本公司控股股东张三及李四计划减持不超过公司总股本的1%。", None),
    ("本公司控股股东张三、李四计划减持不超过公司总股本的1%。", None),
    ("本公司控股股东与一致行动人计划减持不超过公司总股本的1%。", None),
    ("本公司控股股东计划减持不超过公司总股本的101%。", None),
    ("本公司控股股东计划减持不超过公司总股本的1%；计划减持股份，占公司总股本的2%。", None),
    ("本公司控股股东计划以集中竞价减持股份，占公司总股本的0.6%；计划减持不超过总股本1%。", None),
    ("本公司控股股东计划减持股份；本公司计划减持不超过总股本的1%。", None),
    ("本公司控股股东拟以集中竞价减持股份，其持有股份占本公司总股本的23.78%。", None),
))
def test_planned_total_capital_wording_retains_subject_denominator_and_ambiguity_guards(body, ratio):
    result = ExtractCompanyEvents(
        (_document("wording-plan", "控股股东减持计划公告", body),), AT, CompanyEventPolicy(),
    )
    assert result.risks[0].reduction_plan.planned_ratio_pct == ratio


@pytest.mark.parametrize("intent", ("不计划", "尚未计划", "不拟"))
def test_negated_new_reduction_wording_does_not_create_a_risk(intent):
    result = ExtractCompanyEvents(
        (_document("negated-plan", "控股股东减持计划公告", f"本公司控股股东{intent}减持不超过总股本1%。"),),
        AT, CompanyEventPolicy(),
    )
    assert result.risks == ()


@pytest.mark.parametrize("negation", ("不", "尚未", "不再"))
def test_trade_method_cannot_consume_a_negated_reduction_action(negation):
    result = ExtractCompanyEvents(
        (_document(
            "negated-method-plan", "控股股东减持计划公告",
            f"本公司控股股东计划以集中竞价方式{negation}减持股份，占公司总股本的1%。",
        ),), AT, CompanyEventPolicy(),
    )
    assert result.risks == ()


@pytest.mark.parametrize(("amount", "expected"), (
    ("合同额：人民币3340000.00元", 3_340_000),
    ("合同额（不含税）为人民币334万元", 3_340_000),
    ("合同额：3340000.00元", None),
    ("合同额：美元3340000.00元", None),
    ("合同额：人民币3340000.00元；合同金额：人民币3350000.00元", None),
    ("合同额：人民币3340000.00元；合同额：人民币3340000.00元", None),
))
def test_contract_amount_wording_keeps_currency_uniqueness_and_missing_basis(amount, expected):
    result = ExtractCompanyEvents(
        (_document("wording-contract", "关于签订经营合同的公告", f"本公司已签订正式合同。{amount}。"),),
        AT, CompanyEventPolicy(),
    )
    event, = result.positives
    assert isinstance(event.measurements, ContractMeasurements)
    assert event.measurements.contract_amount_cny == expected
    assert event.measurements.reference_annual_revenue_cny is None
    assert event.measurements.amount_to_revenue_pct is None
    assert event.measurements.amount_basis == "unknown"
    assert event.positive_level == 0 and event.level_milestones == ()


@pytest.mark.parametrize("decision", (
    "本公司拟签订正式合同。",
    "本公司已签署框架协议。",
    "本公司已签订正式合同，尚需审批后生效。",
))
def test_new_contract_amount_does_not_override_binding_requirements(decision):
    result = ExtractCompanyEvents(
        (_document("pending-contract", "关于签订经营合同的公告", decision + "合同额：人民币3340000.00元。"),),
        AT, CompanyEventPolicy(),
    )
    event, = result.positives
    assert event.measurements.contract_amount_cny == 3_340_000
    assert not event.measurements.binding_confirmed
    assert event.status is CompanyPositiveStatus.PLANNED
    assert event.positive_level == 0 and event.level_milestones == ()


def test_new_contract_amount_cannot_adopt_a_subsidiary_contract():
    result = ExtractCompanyEvents(
        (_document(
            "subsidiary-contract", "关于签订经营合同的公告",
            "本公司董事会保证公告真实。子公司已签订正式合同。合同额：人民币3340000.00元。",
        ),), AT, CompanyEventPolicy(),
    )
    assert result.positives == ()
