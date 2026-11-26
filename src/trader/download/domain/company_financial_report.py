"""Bind quarterly financial cells to the cumulative period and their source text."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from trader.download.domain.company_announcement_numbers import ExtractCompanyAnnouncementNumbers
from trader.download.domain.company_evidence import CompanyDocument, EvidenceMarket

CompanyFinancialReportField = Literal["basic_eps", "roe_pct", "core_profit_growth_pct", "revenue_growth_pct"]
CompanyFinancialReportState = Literal["numeric", "not_applicable", "not_disclosed", "not_meaningful", "unquantified", "unsupported"]
COMPANY_FINANCIAL_REPORT_FIELDS: tuple[CompanyFinancialReportField, ...] = (
    "basic_eps", "roe_pct", "core_profit_growth_pct", "revenue_growth_pct",
)
_TITLE = r"(20[0-9]{2})年(?:第三季度|三季度|三季)报告(?:全文|正文)?$"
_HEADING = r"主要会计数据和财务指标"
_END = r"[（(][二三][）)]\s*非经常性损益"
_CELL = r"(?:[-+]?[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?[%％]?|[-—－]{1,2}|/|不适用|(?:增加|减少)\s*[-+]?[0-9.]+\s*(?:个(?:百(?:分(?:点)?)?)?)?)"
_EXPLANATION = r"[^。\n]{0,100}(?:加权平均净资产收益率[^。]{0,160}(?:没有披露|不具备参考性|不具备经济参考意义|不能代表)|净资产[^。]{0,100}(?:为负|负数)[^。]{0,180}(?:不具备|未列示))[^。]{0,160}[。]?"
_CURRENT_COLUMN = r"本报告期(?!比|末)"
_YTD_COLUMN = r"年初至报告期(?:末(?!比)|(?=\s{2,}|$))"
_INVALID_UNITS = r"美元|港元|欧元|万元|亿元|千元"
_INCOME_SECTION = r"合并\s*利润表([\s\S]{0,4500}?)\n\s*二[、．]营业总成本"
_INCOME_AMOUNT = r"[-+]?[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?"
_ROE_ANCHOR = r"(?m)^[^\S\n]*" + r"\s*".join("加权平均净资产收益率") + r"(?=\s|[（(]|$)"
_WRAPPED_ROE_DELTA = r"(?:增加|减少)\s*[-+]?[0-9]+(?:\.[0-9]+)?\s*个"
_WRAPPED_ROE_HEAD = r"\s*加权平均净资产收益率\s{2,}(" + _WRAPPED_ROE_DELTA + r")\s{2,}(" + _WRAPPED_ROE_DELTA + r")\s*"
_WRAPPED_ROE_VALUES = r"\s*([-+]?[0-9]+(?:\.[0-9]+)?[%％]?)\s{2,}([-+]?[0-9]+(?:\.[0-9]+)?[%％]?)\s*"
_WRAPPED_ROE_UNITS = r"\s*[（(][%％][）)]\s{2,}(百分点)\s{2,}(百分点)\s*"
_LABELS: tuple[tuple[CompanyFinancialReportField, str, int], ...] = (
    ("revenue_growth_pct", r"(?<!占)营业(?:总)?收入(?:[（(]元[）)])?", 3),
    ("core_profit_growth_pct", r"归属于上市公司股东的扣除非经常性损益的净利润(?:[（(]元[）)])?", 3),
    ("basic_eps", r"基本每股收益[（(]元/股[）)]", 2),
    ("roe_pct", r"加权平均净资产收益率(?:[（(][%％][）)])?", 2),
)
COMPANY_FINANCIAL_REPORT_DIGEST = hashlib.sha256(
    repr((_TITLE, _HEADING, _END, _CELL, _LABELS, _EXPLANATION, _CURRENT_COLUMN, _YTD_COLUMN,
          _INVALID_UNITS, _INCOME_SECTION, _INCOME_AMOUNT, _ROE_ANCHOR,
          _WRAPPED_ROE_HEAD, _WRAPPED_ROE_VALUES, _WRAPPED_ROE_UNITS)).encode(),
).digest()


@dataclass(frozen=True, slots=True)
class CompanyFinancialReportRequest:
    code: str
    report_date: date
    disclosed_at: datetime

    def __post_init__(self) -> None:
        EvidenceMarket(self.code)
        if ((self.report_date.month, self.report_date.day) not in {(9, 30), (12, 31)}
            or self.disclosed_at.utcoffset() is None or self.report_date >= self.disclosed_at.date()):
            raise ValueError("company_financial_report_request_invalid")


@dataclass(frozen=True, slots=True)
class CompanyFinancialReportCell:
    field: CompanyFinancialReportField
    value: float | None
    state: CompanyFinancialReportState
    raw_value: str
    statement: str
    basis_statement: str | None = None


@dataclass(frozen=True, slots=True)
class CompanyFinancialReport:
    code: str
    report_date: date
    cells: tuple[CompanyFinancialReportCell, ...]


def IsCompanyFinancialReportTitle(title: str, report_date: date) -> bool:
    compact = "".join(title.split())
    if report_date.month == 12:
        return re.search(rf"{report_date.year}年年度报告", compact) is not None
    return re.search(rf"{report_date.year}年(?:第三季度|三季度|三季)报告", compact) is not None


def ExtractCompanyFinancialReport(document: CompanyDocument) -> CompanyFinancialReport | None:
    """Read supported four-column layout text; native column streams stay unsupported."""
    title = "".join(document.disclosure.title.split())
    matched = re.search(_TITLE, title)
    if (matched is None or not document.checked or document.terminal_failure
        or document.disclosure.kind != "announcement" or document.disclosure.source not in {"cninfo", "sse", "szse", "bse"}
        or len(document.disclosure.codes) != 1 or any(word in title for word in ("修订", "更正", "英文", "取消"))
        or not _issuer_matches(document)):
        return None
    year = int(matched[1])
    period = date(year, 9, 30)
    if (period >= document.disclosure.published_at.date()
        or {int(match[1]) for match in re.finditer(r"(20[0-9]{2})年第三季度报告", "".join(document.text[:2500].split()))} != {year}):
        return None
    headings = tuple(re.finditer(_HEADING, "".join(document.text[:12000].split())))
    start = re.search(r"主要\s*会计\s*数据\s*和\s*财务\s*指标", document.text[:12000])
    if not headings or start is None:
        return None
    end = re.search(_END, document.text[start.end():start.end() + 14000])
    if end is None:
        return None
    table = document.text[start.end():start.end() + end.start()]
    lines = table.splitlines()
    rows: list[tuple[int, tuple[str, ...]]] = []
    labels: list[str] = []
    label_lines: list[int] = []
    first_values = None
    metric_end = len(lines)
    for index, line in enumerate(lines):
        if first_values is not None and ("本报告期末" in "".join(line.split()) or "上年度末" in "".join(line.split())):
            metric_end = index
            break
        line = re.sub(r"(?<=[0-9%％]) (?=[-+]?[0-9][0-9,.]*[%％](?:\s|$))", "  ", line)
        parts = tuple(part.strip() for part in re.split(r"\s{2,}", line.strip()) if part.strip())
        values = tuple(part for part in parts if re.fullmatch(_CELL, part))
        if len(values) == 4 and parts[-4:] == values:
            rows.append((index, values))
            first_values = index if first_values is None else first_values
            label = "".join(parts[:-4])
        elif not values:
            label = "".join(parts)
        else:
            label = ""
        compact = "".join(label.split())
        labels.extend(compact)
        label_lines.extend([index] * len(compact))
    if first_values is None:
        return None
    header = "".join("".join(lines[:first_values]).split())
    current_columns = tuple(match.start() for line in lines[:first_values] for match in re.finditer(_CURRENT_COLUMN, line))
    year_columns = tuple(match.start() for line in lines[:first_values] for match in re.finditer(_YTD_COLUMN, line))
    if ("本报告期" not in header or "年初至报告期末" not in header
        or "上年同期" not in header or "增减" not in header
        or not current_columns or not year_columns or min(current_columns) >= min(year_columns)
        or re.search(_INVALID_UNITS, header)
        or re.search(r"[√✓☑\uf052]是", header)):
        return None
    joined = "".join(labels)
    explanation = re.search(_EXPLANATION, table)
    cells: list[CompanyFinancialReportCell] = []
    for field, pattern, column in _LABELS:
        found = tuple(re.finditer(pattern, joined))
        if len(found) != 1:
            continue
        label_match = found[0]
        first, last = label_lines[label_match.start()], label_lines[label_match.end() - 1]
        matching = tuple(row for row in rows if first <= row[0] <= last)
        if not matching:
            matching = tuple(row for row in rows if row[0] == last + 1)
        if len(matching) != 1:
            continue
        row_index, values = matching[0]
        basis = None
        if field == "revenue_growth_pct" and "营业总收入" not in label_match[0]:
            basis = _total_revenue_basis(document.text, year, values[2])
            if basis is None:
                continue
        raw = values[column]
        if field in {"core_profit_growth_pct", "revenue_growth_pct"} and re.fullmatch(r"[-+0-9,.]+", raw) and not re.search(r"[（(][%％][）)]", header):
            continue
        if field == "roe_pct" and re.fullmatch(r"[-+0-9,.]+", raw) and re.search(r"加权平均净资产收益率[（(][%％][）)]", joined) is None:
            continue
        statement = "\n".join(lines[max(0, first - 1):max(last, row_index) + 1])
        if len(statement) > 1000:
            continue
        normalized = raw.replace(",", "").rstrip("%％")
        state: CompanyFinancialReportState
        value: float | None = None
        if raw == "不适用":
            state = "not_applicable"
        elif re.fullmatch(r"[-—－]{1,2}|/", raw):
            state = "unquantified"
        elif re.fullmatch(r"[-+]?[0-9]+(?:\.[0-9]+)?", normalized):
            value = float(normalized)
            if not math.isfinite(value):
                continue
            state = "numeric"
        else:
            continue
        cells.append(CompanyFinancialReportCell(field, value, state, raw, statement, basis))
    wrapped_roe = _wrapped_roe(lines[:metric_end])
    if len(tuple(re.finditer(_ROE_ANCHOR, "\n".join(lines[:metric_end])))) > 1 or (
        wrapped_roe is not None and any(cell.field == "roe_pct" for cell in cells)
    ):
        cells = [cell for cell in cells if cell.field != "roe_pct"]
    elif wrapped_roe is not None:
        cells.append(wrapped_roe)
    if explanation is not None:
        # A printed positive ROE can be explicitly declared economically meaningless.
        # Preserve that statement rather than promoting it to usable numeric evidence.
        previous = next((cell for cell in cells if cell.field == "roe_pct"), None)
        statement = explanation[0]
        if len(statement) <= 1000:
            cells = [cell for cell in cells if cell.field != "roe_pct"]
            cells.append(CompanyFinancialReportCell(
                "roe_pct", None, "not_disclosed" if "没有披露" in explanation[0] else "not_meaningful",
                previous.raw_value if previous is not None else "未披露", statement,
            ))
    return CompanyFinancialReport(document.disclosure.codes[0], period, tuple(cells))


def _wrapped_roe(lines: list[str]) -> CompanyFinancialReportCell | None:
    candidates: list[CompanyFinancialReportCell] = []
    for index, line in enumerate(lines[:-2]):
        head = re.fullmatch(_WRAPPED_ROE_HEAD, line)
        if head is None:
            continue
        values = re.fullmatch(_WRAPPED_ROE_VALUES, lines[index + 1])
        units = re.fullmatch(_WRAPPED_ROE_UNITS, lines[index + 2])
        if values is None or units is None:
            continue
        # The two rates and their change columns must alternate horizontally.
        if not (values.end(1) < head.start(1) < head.end(1) < values.start(2)
                and values.end(2) < head.start(2)
                and all(head.start(column) <= units.start(column) < head.end(column) for column in (1, 2))):
            continue
        statement = "\n".join(lines[index:index + 3])
        raw = values[2]
        value = float(raw.rstrip("%％"))
        if len(statement) <= 1000 and math.isfinite(value):
            candidates.append(CompanyFinancialReportCell("roe_pct", value, "numeric", raw, statement))
    return candidates[0] if len(candidates) == 1 else None


def _total_revenue_basis(text: str, year: int, cumulative: str) -> str | None:
    sections = tuple(re.finditer(_INCOME_SECTION, text))
    if len(sections) != 1:
        return None
    section = sections[0][1]
    total = tuple(re.finditer(r"(?m)^\s*一[、．]营业总收入\s+(" + _INCOME_AMOUNT + r")\s+(" + _INCOME_AMOUNT + r")\s*$", section))
    income = tuple(re.finditer(r"(?m)^\s*其中[：:]营业收入\s+(" + _INCOME_AMOUNT + r")\s+(" + _INCOME_AMOUNT + r")\s*$", section))
    if len(total) != 1 or len(income) != 1:
        return None
    header = "".join(section[:total[0].start()].split())
    if (not re.search(rf"{year}年前三季度.*{year - 1}年前三季度", header)
        or "单位：元币种：人民币" not in header or re.search(_INVALID_UNITS, header)):
        return None
    try:
        main = float(cumulative.replace(",", ""))
        current, prior = tuple(float(total[0][i].replace(",", "")) for i in (1, 2))
        reported = tuple(float(income[0][i].replace(",", "")) for i in (1, 2))
    except ValueError:
        return None
    statement = section[:income[0].end()].strip()
    return statement if (len(statement) <= 1000 and main == current and (current, prior) == reported
                         and all(math.isfinite(value) for value in (main, current, prior))) else None


def _issuer_matches(document: CompanyDocument) -> bool:
    known = ExtractCompanyAnnouncementNumbers(document.text).issuer_codes
    if known:
        return known == document.disclosure.codes
    header = document.text[:6000].partition("本公司")[0]
    codes = tuple(sorted({match[1] for match in re.finditer(r"(?m)^\s*(?:证券|股票|公司)代码[：:]\s*([0-9]{6})(?![0-9])", header)}))
    return codes == document.disclosure.codes


__all__ = [
    "COMPANY_FINANCIAL_REPORT_DIGEST",
    "COMPANY_FINANCIAL_REPORT_FIELDS",
    "CompanyFinancialReportRequest",
    "CompanyFinancialReportCell",
    "CompanyFinancialReport",
    "CompanyFinancialReportField",
    "CompanyFinancialReportState",
    "IsCompanyFinancialReportTitle",
    "ExtractCompanyFinancialReport",
]
