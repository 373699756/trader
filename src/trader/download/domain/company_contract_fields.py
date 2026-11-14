"""One parser owns contract measurements and the source statements proving them."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from trader.download.domain.company_events import ContractMeasurements
from trader.download.domain.company_field_results import CompanyFieldName, CompanyFieldUnit


COMPANY_CONTRACT_FIELD_RULES = (
    ("contract_amount", "explicit_cny_contract_amount:3"),
    ("annual_revenue", "explicit_cny_annual_operating_revenue:1"),
    ("revenue_report_date", "explicit_contract_revenue_year:1"),
    ("revenue_disclosed_at", "explicit_annual_report_disclosure:1"),
    ("contract_revenue_pct", "comparable_contract_revenue_ratio:1"),
    ("amount_basis", "explicit_comparable_contract_basis:1"),
)


@dataclass(frozen=True, slots=True)
class CompanyContractField:
    field: CompanyFieldName
    value: float | str | date | datetime
    statements: tuple[tuple[str, CompanyFieldUnit], ...]


@dataclass(frozen=True, slots=True)
class CompanyContractFields:
    measurements: ContractMeasurements
    fields: tuple[CompanyContractField, ...]


def ExtractCompanyContractFields(body: str, published: datetime) -> CompanyContractFields:
    """Parse verified, whitespace-compacted subject text and retain its statements."""
    if published.utcoffset() is None:
        raise ValueError("company_contract_fields_time_invalid")
    binding = any(
        marker in body for marker in ("合同已生效", "合同自签署之日起生效", "已签订正式合同", "已签署正式合同")
    ) and not any(marker in body for marker in ("框架协议", "意向协议", "中标候选", "尚未生效"))
    binding = binding and re.search(r"(?:尚需|须经|需经)[^。；]{0,40}生效", body) is None
    amount = _single_measurement(
        r"(?:合同额|(?:合同|协议)(?:总)?金额)(?:[（(](?:不含税|含税)[）)])?(?:为|约为)?[：:]?人民币([0-9]+(?:[,，][0-9]{3})*(?:\.[0-9]+)?)(亿|万)?元", body,
    )
    revenue = _single_measurement(
        r"(20[0-9]{2})年度(?:经审计的)?营业收入(?:为|合计)?[：:]?人民币([0-9]+(?:[,，][0-9]{3})*(?:\.[0-9]+)?)(亿|万)?元", body,
    )
    disclosure = _annual_report_disclosure(body, int(revenue[1]), published) if revenue else None
    disclosed = disclosure[0] if disclosure is not None else None
    amount_net = _net_statement(body, amount) if amount is not None else None
    revenue_net = _net_statement(body, revenue) if revenue is not None else None
    comparable = amount_net is not None and revenue_net is not None and disclosed is not None
    contract_amount = float(amount[1].replace(",", "").replace("，", "")) * _amount_unit(amount[2]) if amount else None
    annual_revenue = float(revenue[2].replace(",", "").replace("，", "")) * _amount_unit(revenue[3]) if revenue else None
    references = tuple(re.finditer(r"(20[0-9]{2})(?:年度|年)(?:经审计的?)?营业收入", body))
    reference_years = {int(match[1]) for match in references}
    reference_date = date(int(revenue[1]), 12, 31) if revenue else (
        date(next(iter(reference_years)), 12, 31) if len(reference_years) == 1 else None
    )
    ratio = 100 * contract_amount / annual_revenue if (
        comparable and contract_amount is not None and annual_revenue is not None and annual_revenue > 0
    ) else None
    measurements = ContractMeasurements(
        contract_amount, annual_revenue, reference_date, disclosed, ratio, binding,
        "comparable" if comparable else "unknown",
    )
    fields: list[CompanyContractField] = []
    if amount is not None and contract_amount is not None:
        fields.append(CompanyContractField("contract_amount", contract_amount, ((amount[0], "cny"),)))
    if revenue is not None and annual_revenue is not None and reference_date is not None and reference_date < published.date():
        fields.append(CompanyContractField("annual_revenue", annual_revenue, ((revenue[0], "cny"),)))
    year_statement = revenue[0] if revenue is not None else references[0][0] if references else None
    if reference_date is not None and reference_date < published.date() and year_statement is not None:
        fields.append(CompanyContractField("revenue_report_date", reference_date, ((year_statement, "date"),)))
    if disclosure is not None:
        fields.append(CompanyContractField("revenue_disclosed_at", disclosure[0], ((disclosure[1], "time"),)))
    if amount_net is not None and revenue_net is not None and disclosure is not None:
        inputs: tuple[tuple[str, CompanyFieldUnit], ...] = (
            (amount_net, "cny"), (revenue_net, "cny"), (disclosure[1], "time"),
        )
        fields.append(CompanyContractField("amount_basis", "comparable", inputs))
        if ratio is not None:
            fields.append(CompanyContractField("contract_revenue_pct", ratio, inputs))
    return CompanyContractFields(measurements, tuple(fields))


def _single_measurement(pattern: str, body: str) -> re.Match[str] | None:
    matches = tuple(re.finditer(pattern, body))
    return matches[0] if len(matches) == 1 else None


def _net_statement(body: str, match: re.Match[str]) -> str | None:
    start = max(body.rfind(separator, 0, match.start()) for separator in ("；", "。", "，", ",")) + 1
    ends = tuple(body.find(separator, match.end()) for separator in ("；", "。", "，", ","))
    end = min((position for position in ends if position >= 0), default=len(body))
    clause = body[start:end]
    return clause if "不含税" in clause and "含税" not in clause.replace("不含税", "") else None


def _annual_report_disclosure(body: str, year: int, published: datetime) -> tuple[datetime, str] | None:
    match = _single_measurement(
        rf"(?:{year}年?年度报告|该年度报告|上述年度报告)于(20\d{{2}})[年/-](\d{{1,2}})[月/-](\d{{1,2}})日?(?:披露|公告)", body,
    )
    if match is None:
        return None
    try:
        disclosed = datetime(int(match[1]), int(match[2]), int(match[3]), tzinfo=published.tzinfo)
    except ValueError:
        return None
    return (disclosed, match[0]) if date(year, 12, 31) < disclosed.date() < published.date() else None


def _amount_unit(unit: str | None) -> int:
    return 100_000_000 if unit == "亿" else 10_000 if unit == "万" else 1


__all__ = [
    "COMPANY_CONTRACT_FIELD_RULES",
    "CompanyContractField",
    "CompanyContractFields",
    "ExtractCompanyContractFields",
]
