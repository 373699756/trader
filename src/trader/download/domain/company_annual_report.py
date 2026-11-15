"""Read an annual report's current-year revenue column without borrowing comparatives."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date

from trader.download.domain.company_announcement_numbers import ExtractCompanyAnnouncementNumbers
from trader.download.domain.company_evidence import CompanyDocument


_TITLE = r"(20[0-9]{2})年年度报告(?:摘要)?$"
_COLUMN = (
    r"年(?:度)?(?:本年比上年增减|本期比上年同期增减)(?:[（(][%％][）)])?"
)
_NUMBER = r"([0-9]+(?:[,，][0-9]{3})*\.[0-9]{2})"
_ROW = (r"营业收入(?:[（(]\s*元[）)])?\s*" + _NUMBER + r"\s+" + _NUMBER
        + r"\s+[-+]?[0-9]+\.[0-9]{2}[%％]?\s+" + _NUMBER + r"(?=\s|$)")
_UNIT = r"单位[：:]?元(?:币种[：:]?人民币)?"
_HEADING = r"主要会计数据|主要财务指标"
_FOREIGN_UNIT = r"美元|港元|欧元|万元|亿元"
_COMPANY_SECTION = r"公司信息(.{0,1500}?)(?:联系人和联系方式|二、|二[、．])"
_COMPANY_CODE = r"(?:股票|证券)代码[：:]?([0-9]{6})(?![0-9])"
COMPANY_ANNUAL_REPORT_DISCLOSURE_RULE = "official_annual_catalog_disclosure"
COMPANY_ANNUAL_REPORT_DIGEST = hashlib.sha256(
    "\0".join((_TITLE, _COLUMN, _ROW, _UNIT, _HEADING, _FOREIGN_UNIT, _COMPANY_SECTION, _COMPANY_CODE)).encode(),
).digest()


@dataclass(frozen=True, slots=True)
class CompanyAnnualReportRevenue:
    report_date: date
    revenue: float
    statement: str


def ExtractCompanyAnnualReportRevenue(document: CompanyDocument) -> CompanyAnnualReportRevenue | None:
    """Require the supported annual table, issuer header, year and yuan unit together."""
    title = "".join(document.disclosure.title.split())
    year_match = re.search(_TITLE, title)
    if (year_match is None or not document.checked or document.terminal_failure
        or document.disclosure.kind != "announcement" or document.disclosure.source not in {"cninfo", "sse", "szse", "bse"}
        or len(document.disclosure.codes) != 1 or any(word in title for word in ("更正", "修订", "英文", "取消"))
        or not _issuer_matches(document)):
        return None
    year = int(year_match[1])
    report_date = date(year, 12, 31)
    if report_date >= document.disclosure.published_at.date():
        return None
    header_years = {int(match[1]) for match in re.finditer(
        r"(20[0-9]{2})年年度报告", "".join(document.text[:2048].split()),
    )}
    if header_years != {year}:
        return None
    columns = (
        rf"{year}年{year - 1}" + _COLUMN + rf"{year - 2}年营业收入(?:[（(]元[）)])?"
    )
    results: list[CompanyAnnualReportRevenue] = []
    for match in re.finditer(_ROW, document.text):
        prefix = "".join(document.text[max(0, match.start() - 2000):match.start()].split())[-900:]
        row = "".join(match[0].split())
        units = tuple(re.finditer(_UNIT, prefix))
        if ((not units and re.match(r"营业收入[（(]元[）)]", row) is None) or re.search(columns, prefix + row) is None
            or not re.search(_HEADING, prefix)
            or re.search(_FOREIGN_UNIT, prefix[units[-1].start():] if units else prefix) is not None):
            continue
        start = units[-1].start() if units else tuple(re.finditer(_HEADING, prefix))[-1].start()
        statement = prefix[start:] + row
        revenue = float(match[1].replace(",", "").replace("，", ""))
        if len(statement) <= 900 and 0 <= revenue < float("inf"):
            results.append(CompanyAnnualReportRevenue(report_date, revenue, statement))
    return results[0] if len(results) == 1 else None


def _issuer_matches(document: CompanyDocument) -> bool:
    header = ExtractCompanyAnnouncementNumbers(document.text).issuer_codes
    if header:
        return header == document.disclosure.codes
    if "摘要" in document.disclosure.title:
        return False
    sections = tuple(re.finditer(_COMPANY_SECTION, "".join(document.text[:30000].split())))
    if len(sections) != 1:
        return False
    codes = tuple(sorted({match[1] for match in re.finditer(_COMPANY_CODE, sections[0][1])}))
    return codes == document.disclosure.codes


__all__ = [
    "COMPANY_ANNUAL_REPORT_DISCLOSURE_RULE",
    "COMPANY_ANNUAL_REPORT_DIGEST",
    "CompanyAnnualReportRevenue",
    "ExtractCompanyAnnualReportRevenue",
]
