"""Admit report values backed by an exact supplier row and issuer original."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from trader.download.domain.company_evidence import CompanyDocument, CompanyFinancial, Disclosure
from trader.download.domain.company_financial_report import CompanyFinancialReportCell, CompanyFinancialReportField, ExtractCompanyFinancialReport
from trader.download.domain.company_source_records import CompanyFinancialSourceObservation

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_METRICS = re.compile(r"主要\s*会计\s*数据\s*和\s*财务\s*指标")
_METRICS_END = re.compile(r"[（(][二三][）)]\s*非经常性损益")
_AMOUNT = r"[-+]?[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?"
_PERCENT = _AMOUNT + r"[%％]?"
_PROFIT_LABEL = r"归属于上市公司股东(?:的)?净利润(?:[（(]元[）)])?"
_CORE_LABEL = r"归属于上市公司股东(?:的)?扣除非经常性损益(?:的)?净利润(?:[（(]元[）)])?"
_NATIVE_AMOUNT = re.compile(r"单位[:：]元币种[:：]人民币")


@dataclass(frozen=True, slots=True)
class CompanyFinancialRevisionProof:
    """Authority evidence closing one issuer's original and correction window."""

    original: Disclosure
    report_date: date
    checked_from: date
    checked_through: datetime
    source_record_hash: str
    authority_evidence_hash: bytes

    def __post_init__(self) -> None:
        if (self.checked_through.utcoffset() is None or self.original.published_at > self.checked_through
            or self.checked_from > self.report_date + timedelta(days=1)
            or self.checked_from > self.original.published_at.astimezone(_SHANGHAI).date()
            or len(self.original.codes) != 1 or self.original.kind != "announcement"
            or self.original.source not in {"cninfo", "sse", "szse", "bse"}
            or re.fullmatch(r"[0-9a-f]{64}", self.original.digest) is None
            or re.fullmatch(r"[0-9a-f]{64}", self.source_record_hash) is None or len(self.authority_evidence_hash) != 32):
            raise ValueError("company_financial_revision_proof_invalid")


@dataclass(frozen=True, slots=True)
class CompanyFinancialAdmission:
    """Visible values backed by exact supplier, issuer and revision evidence."""

    report: CompanyFinancial
    supplier: CompanyFinancialSourceObservation
    body_hash: bytes
    revision: CompanyFinancialRevisionProof

    def __post_init__(self) -> None:
        original = self.revision.original
        if ((self.report.code, self.report.report_date) != self.supplier.business_key
            or (self.report.code,) != original.codes or self.report.report_date != self.revision.report_date
            or self.report.published_at != original.published_at or self.supplier.record_hash != self.revision.source_record_hash
            or len(self.body_hash) != 32):
            raise ValueError("company_financial_admission_identity_invalid")


def MatchCompanyFinancialOriginal(
    supplier: CompanyFinancialSourceObservation,
    document: CompanyDocument,
    cutoff: datetime,
) -> CompanyFinancial | None:
    """Compare cumulative cells in a supported original, before revision admission."""
    row = supplier.financial
    publication = document.disclosure.published_at
    notice_day = supplier.source_notice_at.astimezone(_SHANGHAI).date()
    if cutoff.utcoffset() is None:
        raise ValueError("company_financial_admission_cutoff_invalid")
    if (
        not document.checked
        or document.terminal_failure
        or publication.astimezone(_SHANGHAI).date() != notice_day
        or publication > cutoff
        or supplier.observed_at > cutoff
        or supplier.source_updated_at is None
        or supplier.source_updated_at.astimezone(_SHANGHAI).date() != notice_day
    ):
        return None
    original = ExtractCompanyFinancialReport(document)
    if original is None or (original.code, original.report_date) != supplier.business_key:
        return None
    amounts = _cumulative_profits(document.text)
    if amounts is None:
        return None
    total, core, growth = amounts
    if row.parent_net_profit is None or row.core_net_profit is None or (
        Decimal(str(row.parent_net_profit)) != total or Decimal(str(row.core_net_profit)) != core
    ):
        return None

    return CompanyFinancial(
        row.code,
        row.report_date,
        publication,
        _verified_cell(original.cells, "basic_eps", row.basic_eps),
        None,
        _verified_cell(original.cells, "revenue_growth_pct", row.revenue_growth_pct),
        _matching_ratio(row.net_profit_growth_pct, growth) if growth is not None else None,
        _verified_cell(original.cells, "core_profit_growth_pct", row.core_profit_growth_pct),
        _verified_cell(original.cells, "roe_pct", row.roe_pct),
        float(total),
        float(core),
    )


def _verified_cell(
    cells: tuple[CompanyFinancialReportCell, ...], field: CompanyFinancialReportField, value: float | None,
) -> float | None:
    for cell in cells:
        if cell.field == field and cell.state == "numeric" and cell.value is not None:
            return _matching_ratio(value, Decimal(cell.raw_value.replace(",", "").rstrip("%％")))
    return None


def _matching_ratio(value: float | None, published: Decimal) -> float | None:
    """Retain the typed supplier value only if its displayed precision agrees."""
    if value is None:
        native = float(published)
        return native if math.isfinite(native) else None
    observed = Decimal(str(value))
    if observed == published:
        return value
    exponent = published.as_tuple().exponent
    if not isinstance(exponent, int) or len(published.as_tuple().digits) > 25:
        return None
    quantum = Decimal(1).scaleb(exponent)
    if abs(observed - published) >= quantum or observed.quantize(quantum, rounding=ROUND_HALF_UP) != published:
        return None
    return value


def _cumulative_profits(text: str) -> tuple[Decimal, Decimal, Decimal | None] | None:
    heading = _METRICS.search(text, 0, 12000)
    if heading is None:
        return None
    section = text[heading.end():heading.end() + 14000]
    end = _METRICS_END.search(section)
    if end is None:
        return None

    labels: list[str] = []
    label_lines: list[int] = []
    rows: list[tuple[int, tuple[str, ...]]] = []
    lines = section[:end.start()].splitlines()
    for index, line in enumerate(lines):
        if rows and "本报告期末" in "".join(line.split()):
            break
        parts = tuple(re.split(r"\s{2,}", line.strip()))
        if len(parts) >= 4 and all(re.fullmatch(_PERCENT, part) for part in parts[-4:]):
            rows.append((index, parts[-4:]))
            parts = parts[:-4]
        label = "".join("".join(parts).split())
        labels.extend(label)
        label_lines.extend([index] * len(label))
    if not rows:
        return None
    header = "".join("".join(lines[:rows[0][0]]).split())
    yuan_header = _NATIVE_AMOUNT.search(header) is not None
    parent = _read_profit_row(_PROFIT_LABEL, "".join(labels), label_lines, rows, yuan_header)
    core = _read_profit_row(_CORE_LABEL, "".join(labels), label_lines, rows, yuan_header)
    if parent is None or core is None:
        return None
    growth = parent[1]
    percent_unit = growth.endswith(("%", "％")) or re.search(r"[（(][%％][）)]", header) is not None
    return parent[0], core[0], Decimal(growth.replace(",", "").rstrip("%％")) if percent_unit else None


def _read_profit_row(
    pattern: str,
    labels: str,
    label_lines: list[int],
    rows: list[tuple[int, tuple[str, ...]]],
    yuan_header: bool,
) -> tuple[Decimal, str] | None:
    matches = tuple(re.finditer(pattern, labels))
    if len(matches) != 1:
        return None
    match = matches[0]
    if not yuan_header and not match[0].endswith(("元）", "元)")):
        return None
    first, last = label_lines[match.start()], label_lines[match.end() - 1]
    candidates = tuple(values for index, values in rows if first <= index <= last)
    if not candidates:
        candidates = tuple(values for index, values in rows if index == last + 1)
    if len(candidates) != 1 or re.fullmatch(_AMOUNT, candidates[0][2]) is None:
        return None
    values = candidates[0]
    return Decimal(values[2].replace(",", "")), values[3]


__all__ = ["CompanyFinancialAdmission", "CompanyFinancialRevisionProof", "MatchCompanyFinancialOriginal"]
