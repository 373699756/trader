"""Contract source proofs retain independent fields and all derived dependencies."""

from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from trader.download.domain.company_contract_fields import ExtractCompanyContractFields
from trader.download.domain.company_event_extraction import ExtractCompanyFieldEvidence, ExtractCompanyLinkCandidate
from trader.download.domain.company_events import CompanyEventPolicy
from trader.download.domain.company_evidence import CompanyDocument, Disclosure


_AT = datetime(2026, 11, 6, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
_POLICY = CompanyEventPolicy((0.1, 0.3, 0.5), (5.0, 10.0, 20.0), 365, 90, 180)
_BODY = (
    "本公司已签订正式合同，合同已生效。\n"
    "合同金额人民币2亿元（不含税）。\n"
    "2025年度营业收入人民币10亿元（不含税）。\n"
    "2025年年度报告于2026年4月1日披露。"
)


def _document(text=_BODY):
    return CompanyDocument(Disclosure(
        "cninfo", "contract-source", ("600001",), "关于签订重大合同的公告", _AT, "", "source-record",
    ), text, True)


def test_six_contract_fields_share_measurements_and_locate_every_derived_input():
    document = _document()
    candidate = ExtractCompanyLinkCandidate(document, _AT, _POLICY)
    fields = {item.field: item for item in ExtractCompanyFieldEvidence(document, _AT, _POLICY)}
    assert set(fields) == {"contract_amount", "annual_revenue", "revenue_report_date", "revenue_disclosed_at", "contract_revenue_pct", "amount_basis"}
    assert fields["contract_amount"].value == candidate.positive.measurements.contract_amount_cny == 200_000_000
    assert fields["annual_revenue"].value == 1_000_000_000
    assert fields["contract_revenue_pct"].value == candidate.positive.measurements.amount_to_revenue_pct == 20
    assert fields["revenue_report_date"].value == date(2025, 12, 31)
    assert fields["revenue_disclosed_at"].value == datetime(2026, 4, 1, tzinfo=_AT.tzinfo)
    assert fields["amount_basis"].value == "comparable"
    for name in ("contract_revenue_pct", "amount_basis"):
        proofs = fields[name].evidence
        assert len(proofs) == 3
        assert {proof.start_line for proof in proofs} == {2, 3, 4}
        assert all(proof.start_line == proof.end_line and proof.quote in _BODY for proof in proofs)
        assert [proof.unit for proof in proofs] == ["cny", "cny", "time"]
        assert all("不含税" in proof.quote for proof in proofs[:2])


@pytest.mark.parametrize("text", (
    _BODY.replace("（不含税）", "（含税）", 1),
    _BODY.replace("2025年年度报告于2026年4月1日披露。", ""),
    _BODY.replace("2026年4月1日", "2026年11月6日"),
    _BODY.replace("2026年4月1日", "2027年4月1日"),
    _BODY.replace("2026年4月1日", "2026年2月30日"),
))
def test_missing_or_invalid_comparability_keeps_amount_without_certifying_ratio(text):
    fields = {item.field: item for item in ExtractCompanyFieldEvidence(_document(text), _AT, _POLICY)}
    assert fields["contract_amount"].value == 200_000_000
    assert "contract_revenue_pct" not in fields and "amount_basis" not in fields


def test_real_explicit_amount_and_reference_year_do_not_certify_unstated_annual_income():
    # Public notice 1225573372 discloses a CNY contract amount and a 2025
    # operating-income comparison; the separate structured income is not a quote.
    text = "本公司已签订正式合同。合同额：人民币3340000.00元。本次合同签约价占公司2025年度经审计营业收入的1.35%。"
    fields = {item.field: item for item in ExtractCompanyFieldEvidence(_document(text), _AT, _POLICY)}
    assert fields["contract_amount"].value == 3_340_000
    assert fields["revenue_report_date"].value == date(2025, 12, 31)
    assert "annual_revenue" not in fields and "contract_revenue_pct" not in fields


@pytest.mark.parametrize("text", (
    "本公司已签订正式合同。合同金额人民币1亿元。合同金额人民币2亿元。",
    "本公司已签订正式合同。合同金额美元2亿元。",
    "本公司已签订正式合同。预计中标总金额人民币2亿元，尚未签订正式合同。",
    "本公司已签订正式合同。子公司签订合同，合同金额人民币2亿元。",
))
def test_conflicting_foreign_bid_or_other_subject_amount_is_never_certified(text):
    fields = ExtractCompanyFieldEvidence(_document(text), _AT, _POLICY)
    assert not any(item.field == "contract_amount" for item in fields)


def test_zero_revenue_is_a_value_and_never_a_zero_or_infinite_derived_ratio():
    parsed = ExtractCompanyContractFields("".join(_BODY.replace("10亿元", "0元").split()), _AT)
    assert parsed.measurements.reference_annual_revenue_cny == 0
    assert parsed.measurements.amount_to_revenue_pct is None
    assert not any(item.field == "contract_revenue_pct" for item in parsed.fields)


def test_future_reference_year_and_naive_disclosure_cannot_be_certified():
    fields = ExtractCompanyFieldEvidence(_document(_BODY.replace("2025", "2027")), _AT, _POLICY)
    assert not any(item.field in {"annual_revenue", "revenue_report_date", "revenue_disclosed_at", "contract_revenue_pct", "amount_basis"} for item in fields)
    with pytest.raises(ValueError, match="company_contract_fields_time_invalid"):
        ExtractCompanyContractFields("".join(_BODY.split()), _AT.replace(tzinfo=None))


def test_every_derived_statement_must_be_uniquely_locatable_in_complete_text():
    # The matching disclosure is repeated inside another subject's excluded
    # section. A valid measurement alone cannot authorize a source locator.
    text = _BODY + "控股股东材料：2025年年度报告于2026年4月1日披露。"
    fields = ExtractCompanyFieldEvidence(_document(text), _AT, _POLICY)
    assert not any(item.field in {"contract_revenue_pct", "amount_basis", "revenue_disclosed_at"} for item in fields)
    cancelled = replace(_document(), disclosure=replace(_document().disclosure, title="关于终止重大合同的公告"))
    assert not ExtractCompanyFieldEvidence(cancelled, _AT, _POLICY)
