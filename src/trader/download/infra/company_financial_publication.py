"""Prepare exact issuer financial proof and read published material dependencies."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from trader.download.domain.company_evidence import CompanyDocument, CompanyFinancial, Disclosure
from trader.download.domain.company_financial_admission import CompanyFinancialAdmission, CompanyFinancialRevisionProof, MatchCompanyFinancialOriginal
from trader.download.domain.company_financial_report import COMPANY_FINANCIAL_REPORT_DIGEST, COMPANY_FINANCIAL_REPORT_FIELDS, CompanyFinancialReportField, CompanyFinancialReportState
from trader.download.domain.company_source_records import CompanyFinancialField, CompanyFinancialSourceObservation, CompanyFinancialSourceValues
from trader.download.infra.company_fact_inputs import DecodeCompanyDisclosure
from trader.download.infra.company_facts_index import CompanyInputReceipt, ReadCompanyPublicationIdentity
from trader.download.infra.company_facts_protocol import EncodeCompanyTime, RequireCompanyTime
from trader.download.infra.company_financial_observations import ReadCompanyFinancialObservation
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_connection import SqliteConnection

_STATES: tuple[CompanyFinancialReportState, ...] = ("numeric", "not_applicable", "not_disclosed", "not_meaningful", "unquantified", "unsupported")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_PERIODIC = re.compile(r"(20[0-9]{2})年(第一季度|一季度|一季|半年度|中期|第三季度|三季度|三季|年度)报告")
_ORIGINAL_QUARTER = re.compile(r"(?:[\u4e00-\u9fffA-Za-z0-9（）():：·]{0,80})?20[0-9]{2}年(?:第三季度|三季度|三季)报告(?:全文|正文)?")
_CORRECTION = re.compile(r"更正|修订|调整|重述|补充|差错|撤回|取消|作废")
_FINANCIAL = re.compile(r"财务|财报|报表|会计|利润|报告")
_FINANCIAL_STATEMENTS = re.compile(r"定期报告|财报|财务(?:会计)?(?:报告|报表|数据|信息)|会计差错|前期差错")
_ISSUER_PROOF = "financial_issuer_original"
_DIGEST = re.compile(r"[0-9a-f]{64}")
_PERIOD_END = {
    "第一季度": (3, 31), "一季度": (3, 31), "一季": (3, 31), "半年度": (6, 30), "中期": (6, 30),
    "第三季度": (9, 30), "三季度": (9, 30), "三季": (9, 30), "年度": (12, 31),
}


def IsCompanyFinancialDirectoryEvidence(title: str) -> bool:
    """Identify original and correction titles needed by financial admission."""
    normalized = "".join(title.split())
    return _ORIGINAL_QUARTER.fullmatch(normalized) is not None or (
        _CORRECTION.search(normalized) is not None
        and (_period(normalized) is not None or _FINANCIAL_STATEMENTS.search(normalized) is not None)
    )


def _is_financial_correction(title: str) -> bool:
    return _CORRECTION.search(title) is not None and _FINANCIAL.search(title) is not None


def PrepareCompanyFinancialAdmissions(
    collected: SqliteConnection,
    observations: tuple[CompanyFinancialSourceObservation, ...],
    cutoff: datetime,
    *,
    read_document: Callable[[bytes], str],
    check: Callable[[], None],
    revision_proofs: tuple[CompanyFinancialRevisionProof, ...] = (),
) -> tuple[CompanyFinancialAdmission, ...]:
    """Use retained originals with explicit issuer revision proof, without requests."""
    if cutoff.utcoffset() is None:
        raise ValueError("company_financial_admission_cutoff_invalid")
    if not revision_proofs:
        return ()
    cutoff = cutoff.astimezone(_SHANGHAI)
    proofs: dict[tuple[str, date, str], list[CompanyFinancialRevisionProof]] = {}
    for proof in revision_proofs:
        if proof.checked_through == cutoff:
            proofs.setdefault((proof.original.codes[0], proof.report_date, proof.source_record_hash), []).append(proof)
    results = []
    for item in observations:
        check()
        report = item.financial
        matched = proofs.get((*item.business_key, item.record_hash), ())
        if len(matched) != 1 or item.observed_at > cutoff or (report.report_date.month, report.report_date.day) != (9, 30):
            continue
        proof = matched[0]
        start = report.report_date + timedelta(days=1)
        rows = collected.run("""SELECT d.*,c.code,
            (SELECT COUNT(*) FROM disclosure_codes m WHERE m.disclosure_id=d.id) AS member_count,
            (SELECT COUNT(*) FROM document_extractions x WHERE x.disclosure_id=d.id AND x.record_hash=d.record_hash) AS extracted,
            EXISTS(SELECT 1 FROM document_supersessions s WHERE s.disclosure_id=d.id OR s.replacement_id=d.id) AS supersession
            FROM disclosure_codes c JOIN disclosures d ON d.id=c.disclosure_id
            WHERE c.code=? AND d.kind='announcement' AND d.published_at>=? AND d.published_at<=?
            AND (d.title LIKE '%报告%' OR d.title LIKE '%财务%' OR d.title LIKE '%财报%'
                OR d.title LIKE '%报表%' OR d.title LIKE '%会计%' OR d.title LIKE '%利润%')
            ORDER BY d.published_at,d.source,d.source_key""", (report.code, start.isoformat(), cutoff.isoformat())).fetchall()
        originals = []
        for row in rows:
            title = "".join(row["title"].split())
            period = _period(title)
            if (period is not None and period > report.report_date or _is_financial_correction(title)):
                break
            if period == report.report_date and _ORIGINAL_QUARTER.fullmatch(title) is not None:
                originals.append(row)
        else:
            if len(originals) == 1:
                row = originals[0]
                disclosure = DecodeCompanyDisclosure(row)
                digest = row["body_hash"]
                if (disclosure != proof.original or row["member_count"] != 1 or row["extracted"] != 1 or row["supersession"]
                    or not row["body_checked"] or not row["body_retained"] or not isinstance(digest, bytes) or len(digest) != 32):
                    continue
                check()
                try:
                    body = read_document(digest)
                except InterruptedError:
                    raise
                except (OSError, ValueError, EOFError):
                    continue
                if hashlib.sha256(body.encode()).digest() != digest:
                    continue
                check()
                admitted = MatchCompanyFinancialOriginal(item, CompanyDocument(disclosure, body, True), cutoff)
                if admitted is not None:
                    results.append(CompanyFinancialAdmission(admitted, item, digest, proof))
    return tuple(results)


def _period(title: str) -> date | None:
    match = _PERIODIC.search(title)
    return date(int(match[1]), *_PERIOD_END[match[2]]) if match is not None else None


def CompanyFinancialAdmissionReceipts(
    proof: CompanyFinancialAdmission,
    *,
    extraction_hash: bytes,
    published_at: datetime,
) -> tuple[CompanyInputReceipt, ...]:
    """Bind the exact official body, supplier row and admitted values together."""
    original, supplier = proof.revision.original, proof.supplier
    fields = tuple(field for field, value in CompanyFinancialSourceValues(proof.report) if value is not None)
    policy = (f"{_ISSUER_PROOF}:{proof.report.report_date}:{supplier.record_hash}:"
              f"{_issuer_report_digest(proof.report, fields)}:{proof.revision.authority_evidence_hash.hex()}:{','.join(fields)}")
    return (
        CompanyInputReceipt(original.source, original.identity, proof.report.code, original.digest,
            extraction_hash, proof.body_hash, published_at, "valid", _ISSUER_PROOF),
        CompanyInputReceipt(original.source, original.identity, proof.report.code, original.digest,
            hashlib.sha256(policy.encode()).digest(), proof.body_hash, published_at, "field_attempted", policy),
    )


def _issuer_report_digest(report: CompanyFinancial, fields: tuple[CompanyFinancialField, ...]) -> str:
    values = tuple((field, None if value is None else 0.0 if value == 0 else float(value))
                   for field, value in CompanyFinancialSourceValues(report) if field in fields)
    return CanonicalDigest((report.code, report.report_date, EncodeCompanyTime(report.published_at), values))


def _issuer_policy_fields(part: str, report: CompanyFinancial) -> tuple[CompanyFinancialField, ...]:
    requested = part.split(",")
    fields = tuple(field for field, value in CompanyFinancialSourceValues(report) if field in requested and value is not None)
    return fields if ",".join(fields) == part else ()


@dataclass(frozen=True, slots=True)
class CompanyFinancialSourceProof:
    field: CompanyFinancialField
    value: float | None
    state: CompanyFinancialReportState
    source_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompanyFinancialIssuerEvidence:
    supplier_report: CompanyFinancial
    source: str
    completed_fields: tuple[CompanyFinancialSourceProof, ...]


def ReadCompanyFinancialIssuerEvidence(
    collected: SqliteConnection,
    formal: SqliteConnection,
    report: CompanyFinancial,
    observed_at: datetime,
    extraction_hash: bytes,
) -> CompanyFinancialIssuerEvidence | None:
    """Trace an admitted report to its exact supplier row and issuer receipts."""
    if report.published_at > observed_at or (report.report_date.month, report.report_date.day) != (9, 30):
        return None
    prefix = f"{_ISSUER_PROOF}:{report.report_date}:"
    originals = collected.run("""
        SELECT d.source,d.source_key,d.record_hash,d.body_hash,d.title
        FROM disclosure_codes c JOIN disclosures d ON d.id=c.disclosure_id
        WHERE c.code=? AND d.published_at=? AND d.kind='announcement' AND d.body_checked=1
        AND NOT EXISTS(SELECT 1 FROM disclosure_codes other WHERE other.disclosure_id=d.id AND other.code<>c.code)
        AND EXISTS(SELECT 1 FROM document_extractions x WHERE x.disclosure_id=d.id AND x.record_hash=d.record_hash)
        """, (report.code, report.published_at.astimezone(_SHANGHAI).isoformat()))
    matched: dict[tuple[str, str, str, bytes], CompanyFinancialIssuerEvidence] = {}
    for source, key, record_hash, body, title in originals:
        if (not isinstance(body, bytes) or len(body) != 32
            or _ORIGINAL_QUARTER.fullmatch("".join(title.split())) is None):
            continue
        candidates = formal.run("""
            SELECT r.published_at,r.policy_id,r.extraction_hash
            FROM company_input_receipt r JOIN company_input_receipt v
            ON v.source=r.source AND v.source_key=r.source_key AND v.code=r.code AND v.record_hash=r.record_hash
            AND v.body_hash=r.body_hash AND v.published_at<=r.published_at
            WHERE r.source=? AND r.source_key=? AND r.code=? AND r.record_hash=? AND r.body_hash=?
            AND r.outcome='field_attempted' AND r.policy_id GLOB ? AND r.published_at BETWEEN ? AND ?
            AND v.outcome='valid' AND v.extraction_hash=?
            """, (source, key, report.code, record_hash, body, prefix + "*", EncodeCompanyTime(report.published_at),
                   EncodeCompanyTime(observed_at), extraction_hash))
        for stamped, policy, digest in candidates:
            parts = policy[len(prefix):].split(":")
            if (len(parts) != 4 or not all(_DIGEST.fullmatch(value) for value in parts[:3])
                or digest != hashlib.sha256(policy.encode()).digest()):
                continue
            signed_fields = _issuer_policy_fields(parts[3], report)
            if not signed_fields or parts[1] != _issuer_report_digest(report, signed_fields):
                continue
            supplier = ReadCompanyFinancialObservation(collected, report.code, report.report_date, parts[0])
            if supplier is None or supplier.observed_at > RequireCompanyTime(stamped):
                continue
            fields = tuple(CompanyFinancialSourceProof(field, current, "numeric", (key,))
                for (field, current), (_, supplied) in zip(CompanyFinancialSourceValues(report),
                    CompanyFinancialSourceValues(supplier.financial), strict=True)
                if field in signed_fields and supplied is None and current is not None)
            identity = (parts[0], source, key, body)
            previous = matched.get(identity)
            if previous is not None:
                fields = tuple(sorted(set(previous.completed_fields).union(fields), key=lambda item: item.field))
            matched[identity] = CompanyFinancialIssuerEvidence(supplier.financial, source, fields)
    return next(iter(matched.values())) if len(matched) == 1 else None


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
        "AND outcome='field_attempted' AND (policy_id LIKE 'financial_source:%' OR policy_id LIKE 'financial_issuer_original:%')", codes,
    ):
        parts = row[2].split(":")
        if parts[0] == _ISSUER_PROOF:
            if len(parts) != 6 or any(_DIGEST.fullmatch(part) is None for part in parts[2:5]) or not parts[5]:
                raise ValueError("company_financial_source_policy_invalid")
        elif len(parts) != 6 or parts[3] not in COMPANY_FINANCIAL_REPORT_FIELDS or parts[4] not in _STATES:
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

__all__ = ["CompanyFinancialAdmissionReceipts", "CompanyFinancialIssuerEvidence", "CompanyFinancialSourceProof", "CompanyFinancialMaterial", "IsCompanyFinancialDirectoryEvidence", "PrepareCompanyFinancialAdmissions", "ReadCompanyFinancialIssuerEvidence", "ReadCompanyFinancialMaterials", "CompanyFinancialReportValue", "ReadCompanyFinancialSourceProofs"]
