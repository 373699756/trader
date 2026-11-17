"""Read historical financial source proofs without producing PDF financial replacements."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from trader.download.domain.company_evidence import CompanyFinancial, Disclosure
from trader.download.domain.company_financial_report import COMPANY_FINANCIAL_REPORT_DIGEST, COMPANY_FINANCIAL_REPORT_FIELDS, CompanyFinancialReportField, CompanyFinancialReportState
from trader.download.infra.company_fact_inputs import DecodeCompanyDisclosure
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_connection import SqliteConnection

_STATES: tuple[CompanyFinancialReportState, ...] = ("numeric", "not_applicable", "not_disclosed", "not_meaningful", "unquantified", "unsupported")


@dataclass(frozen=True, slots=True)
class CompanyFinancialSourceProof:
    field: CompanyFinancialReportField
    value: float | None
    state: CompanyFinancialReportState
    source_keys: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class _ReportSource:
    disclosure: Disclosure
    body_hash: bytes | None
    ready: bool

@dataclass(frozen=True, slots=True)
class CompanyFinancialMaterial:
    code: str
    source_key: str
    report_date: date

def ReadCompanyFinancialMaterials(formal: SqliteConnection, codes: Sequence[str]) -> tuple[CompanyFinancialMaterial, ...]:
    if not codes:
        return ()
    slots = ",".join("?" for _ in codes)
    materials: list[CompanyFinancialMaterial] = []
    for row in formal.run(
        f"SELECT code,source_key,policy_id FROM company_input_receipt WHERE code IN ({slots}) "
        "AND outcome='field_attempted' AND policy_id LIKE 'financial_source:%'", codes,
    ):
        parts = row[2].split(":")
        if len(parts) != 6 or parts[3] not in COMPANY_FINANCIAL_REPORT_FIELDS or parts[4] not in _STATES:
            raise ValueError("company_financial_source_policy_invalid")
        materials.append(CompanyFinancialMaterial(row[0], row[1], date.fromisoformat(parts[1])))
    return tuple(materials)

def CompanyFinancialReportValue(report: CompanyFinancial, field: CompanyFinancialReportField) -> float | None:
    if field == "basic_eps":
        return report.basic_eps
    if field == "roe_pct":
        return report.roe_pct
    if field == "core_profit_growth_pct":
        return report.core_profit_growth_pct
    return report.revenue_growth_pct

def _sources(collected: SqliteConnection, report: CompanyFinancial, observed_at: datetime) -> tuple[_ReportSource, ...]:
    rows = collected.run(
        "SELECT d.*,c.code FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
        "WHERE c.code=? AND d.kind='announcement' AND d.published_at>=? AND d.published_at<=? "
        "AND (d.title LIKE ? OR d.title LIKE ? OR d.title LIKE ?) "
        "AND (SELECT COUNT(*) FROM disclosure_codes m WHERE m.disclosure_id=d.id)=1 "
        "ORDER BY d.published_at DESC,d.source_key LIMIT 5",
        (report.code, (observed_at.date() - timedelta(days=365)).isoformat(), observed_at.isoformat(),
         f"%{report.report_date.year}%第三季度报告%", f"%{report.report_date.year}%三季度报告%",
         f"%{report.report_date.year}%三季报告%"),
    ).fetchall()
    sources: list[_ReportSource] = []
    for row in rows:
        if row["published_at"] != rows[0]["published_at"]:
            continue
        digest = row["body_hash"]
        if digest is not None and (not isinstance(digest, bytes) or len(digest) != 32):
            raise ValueError("company_financial_report_body_hash_invalid")
        sources.append(_ReportSource(DecodeCompanyDisclosure(row), digest, bool(row["body_checked"] and row["body_retained"])))
    return tuple(sources)

def _prefix(report: CompanyFinancial, sources: Sequence[_ReportSource], field: CompanyFinancialReportField) -> str:
    digest = CanonicalDigest((
        COMPANY_FINANCIAL_REPORT_DIGEST.hex(), report,
        tuple((source.disclosure, source.body_hash.hex() if source.body_hash is not None else None) for source in sources),
    ))
    return f"financial_source:{report.report_date}:{digest}:{field}:"

def _policy(prefix: str, state: CompanyFinancialReportState, value: float | None) -> str:
    normalized = 0.0 if value == 0 else value
    return prefix + state + ":" + CanonicalDigest((state, normalized))

def ReadCompanyFinancialSourceProofs(
    collected: SqliteConnection,
    formal: SqliteConnection,
    source_report: CompanyFinancial,
    published_report: CompanyFinancial | None,
    observed_at: datetime,
) -> tuple[CompanyFinancialSourceProof, ...]:
    """Reuse only exact current sources, original structured input and the sole published value."""
    if ((source_report.report_date.month, source_report.report_date.day) != (9, 30)
        or published_report is None
        or (published_report.code, published_report.report_date, published_report.published_at)
        != (source_report.code, source_report.report_date, source_report.published_at)):
        return ()
    sources = _sources(collected, source_report, observed_at)
    identity = ReadCompanyPublicationIdentity(formal)
    if (not sources or len(sources) > 4 or any(not source.ready or source.body_hash is None
        or source.disclosure.published_at != source_report.published_at for source in sources) or identity is None
        or any(formal.run(
            "SELECT 1 FROM company_input_receipt WHERE source=? AND source_key=? AND code=? AND record_hash=? "
            "AND body_hash=? AND extraction_hash=? AND outcome='valid'",
            (source.disclosure.source, source.disclosure.identity, source_report.code, source.disclosure.digest,
             source.body_hash, identity.extraction_hash),
        ).fetchone() is None for source in sources)):
        return ()
    proofs: list[CompanyFinancialSourceProof] = []
    for field in COMPANY_FINANCIAL_REPORT_FIELDS:
        if CompanyFinancialReportValue(source_report, field) is not None:
            continue
        value = CompanyFinancialReportValue(published_report, field)
        prefix = _prefix(source_report, sources, field)
        for state in _STATES:
            if (state == "numeric") != (value is not None):
                continue
            policy = _policy(prefix, state, value)
            if all(formal.run(
                "SELECT 1 FROM company_input_receipt WHERE source=? AND source_key=? AND code=? AND record_hash=? "
                "AND body_hash=? AND extraction_hash=? AND outcome='field_attempted' AND policy_id=?",
                (source.disclosure.source, source.disclosure.identity, source_report.code, source.disclosure.digest,
                 source.body_hash, hashlib.sha256(policy.encode()).digest(), policy),
            ).fetchone() is not None for source in sources):
                proofs.append(CompanyFinancialSourceProof(field, value, state, tuple(source.disclosure.identity for source in sources)))
                break
    return tuple(proofs)

__all__ = ["CompanyFinancialSourceProof", "CompanyFinancialMaterial", "ReadCompanyFinancialMaterials", "CompanyFinancialReportValue", "ReadCompanyFinancialSourceProofs"]
