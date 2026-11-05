"""One SQL decoding owner for per-company or bounded batch publication link reads."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from trader.download.domain.company_event_extraction import CompanyLinkCandidate, ExtractCompanyLinkCandidate
from trader.download.domain.company_events import CompanyEventPolicy, CompanyPositiveType, CompanyReviewReason, ContractMeasurements
from trader.download.domain.company_evidence import CompanyDocument, Disclosure
from trader.download.infra.company_contract_link_rules import (
    CompanyContractLinkRefresh,
    CompanyContractLinkRule,
    ReadCompanyContractLinkRules,
)
from trader.download.infra.company_link_codec import DecodeCompanyLink
from trader.infra.sqlite_connection import SqliteConnection


@dataclass(frozen=True, slots=True)
class CompanyPublicationLinks:
    code: str
    candidates: tuple[CompanyLinkCandidate, ...]
    reviews: tuple[CompanyReviewReason, ...] = ()
    refreshes: tuple[CompanyContractLinkRefresh, ...] = ()


@dataclass(frozen=True, slots=True)
class _LinkInput:
    code: str
    source: str
    source_key: str
    record_hash: str
    body_hash: bytes | None
    body_retained: bool
    body_checked: bool
    title: str
    published_at: datetime
    url: str
    payload: str


@dataclass(frozen=True, slots=True)
class _ContractRecheck:
    candidate: CompanyLinkCandidate | None
    message: str
    body_verified: bool


def PendingCompanyContractLinkCodes(
    connection: SqliteConnection, codes: Sequence[str], extraction_hash: bytes
) -> tuple[str, ...]:
    """Mark only unverified legacy contract inputs for local publication review."""
    rows = _link_inputs(connection, codes, extraction_hash)
    rules = ReadCompanyContractLinkRules(connection, codes, extraction_hash)
    pending: set[str] = set()
    for row in rows:
        candidate = DecodeCompanyLink(row.payload)
        if candidate.positive is None or candidate.positive.event_type is not CompanyPositiveType.OPERATING_CONTRACT:
            continue
        if _binding(row, extraction_hash) not in rules:
            pending.add(row.code)
    return tuple(sorted(pending))


def ReadCompanyPublicationLinks(
    connection: SqliteConnection,
    codes: Sequence[str],
    extraction_hash: bytes,
    *,
    observed_at: datetime,
    event_policy: CompanyEventPolicy,
    read_document: Callable[[bytes], str],
) -> tuple[CompanyPublicationLinks, ...]:
    """Keep legacy contract payloads behind the current local subject guard.

    Missing local proof never authorizes a network request or a global rule
    change. Hash-verified text can rebuild a candidate; unavailable text leaves
    a typed review and the original formal fact/receipt untouched.
    """
    rows = _link_inputs(connection, codes, extraction_hash)
    if observed_at.utcoffset() is None:
        raise ValueError("company_publication_link_time_invalid")
    rules = ReadCompanyContractLinkRules(connection, codes, extraction_hash)
    payloads: dict[str, list[CompanyLinkCandidate]] = {code: [] for code in codes}
    reviews: dict[str, list[CompanyReviewReason]] = {code: [] for code in codes}
    refreshes: dict[str, list[CompanyContractLinkRefresh]] = {code: [] for code in codes}
    checked: dict[tuple[str, str, str, bytes | None], _ContractRecheck] = {}
    for row in rows:
        candidate = DecodeCompanyLink(row.payload)
        if candidate.positive is None or candidate.positive.event_type is not CompanyPositiveType.OPERATING_CONTRACT:
            payloads[row.code].append(candidate)
            continue
        binding = _binding(row, extraction_hash)
        if (binding is not None and binding in rules and candidate.evidence_id == row.source_key
            and candidate.published_at == row.published_at and row.published_at <= observed_at):
            payloads[row.code].append(candidate)
            continue
        key = (row.source, row.source_key, row.record_hash, row.body_hash)
        if key not in checked:
            checked[key] = _recheck_contract(row, observed_at, event_policy, read_document)
        recheck = checked[key]
        rebuilt, reason = recheck.candidate, recheck.message
        if rebuilt is not None:
            payloads[row.code].append(rebuilt)
            if (_contract_conclusion_conflicts(candidate, rebuilt)
                or candidate.evidence_id != row.source_key or candidate.published_at != row.published_at):
                reason = "旧合同关联候选与当前完整正文的核验结论不一致"
        if reason:
            reviews[row.code].append(CompanyReviewReason("positive", "subject_unverified", row.source_key, observed_at, reason))
        if binding is not None and recheck.body_verified:
            refreshes[row.code].append(CompanyContractLinkRefresh(binding, rebuilt))
    return tuple(CompanyPublicationLinks(code, tuple(payloads[code]), tuple(reviews[code]), tuple(refreshes[code])) for code in codes)


def _contract_conclusion_conflicts(before: CompanyLinkCandidate, after: CompanyLinkCandidate) -> bool:
    if before.subject is not after.subject or before.cancellation != after.cancellation:
        return True
    old, new = before.positive, after.positive
    if old is None or new is None or old.subject_type is not new.subject_type:
        return True
    if (old.event_id != new.event_id or old.event_type is not new.event_type or old.evidence_ids != new.evidence_ids
        or old.first_disclosed_at != new.first_disclosed_at or old.last_disclosed_at != new.last_disclosed_at
        or old.cancelled_at != new.cancelled_at):
        return True
    previous, current = old.measurements, new.measurements
    if not isinstance(previous, ContractMeasurements) or not isinstance(current, ContractMeasurements):
        return True
    if previous.binding_confirmed and not current.binding_confirmed:
        return True
    if previous.amount_basis == "comparable" and current.amount_basis != "comparable":
        return True
    # Missing old measurements are incomplete derivations, not contradictory
    # source facts. A later rule can legitimately recover these same-body values.
    return any(left is not None and left != right for left, right in (
        (previous.contract_amount_cny, current.contract_amount_cny),
        (previous.reference_annual_revenue_cny, current.reference_annual_revenue_cny),
        (previous.reference_report_date, current.reference_report_date),
        (previous.reference_disclosed_at, current.reference_disclosed_at),
        (previous.amount_to_revenue_pct, current.amount_to_revenue_pct),
    ))


def _link_inputs(
    connection: SqliteConnection, codes: Sequence[str], extraction_hash: bytes
) -> tuple[_LinkInput, ...]:
    if len(codes) > 128 or len(set(codes)) != len(codes):
        raise ValueError("company_publication_link_batch_invalid")
    if not codes:
        return ()
    placeholders = ",".join("?" for _code in codes)
    return tuple(_LinkInput(
        row[0], row[1], row[2], row[3], row[4], bool(row[5]), bool(row[6]), row[7], datetime.fromisoformat(row[8]), row[9], row[10]
    ) for row in connection.run(
        "SELECT c.code,d.source,d.source_key,d.record_hash,d.body_hash,d.body_retained,d.body_checked,"
        "d.title,d.published_at,d.url,l.payload FROM document_link_candidates l JOIN disclosures d "
        "ON d.source=l.source AND d.source_key=l.source_key AND d.record_hash=l.record_hash "
        "JOIN disclosure_codes c ON c.disclosure_id=d.id "
        f"WHERE c.code IN ({placeholders}) AND l.extraction_hash=? ORDER BY c.code,d.source,d.source_key",
        (*codes, extraction_hash),
    ))


def _binding(row: _LinkInput, extraction_hash: bytes) -> CompanyContractLinkRule | None:
    if not isinstance(row.body_hash, bytes) or len(row.body_hash) != 32:
        return None
    return CompanyContractLinkRule(
        row.source, row.source_key, row.code, row.record_hash, row.body_hash, extraction_hash,
        hashlib.sha256(row.payload.encode("utf-8")).digest(),
    )


def _recheck_contract(
    row: _LinkInput,
    observed_at: datetime,
    event_policy: CompanyEventPolicy,
    read_document: Callable[[bytes], str],
) -> _ContractRecheck:
    if not row.body_retained or not row.body_checked or not isinstance(row.body_hash, bytes) or len(row.body_hash) != 32:
        return _ContractRecheck(None, "旧合同关联候选缺少可核验的完整本地正文", False)
    try:
        text = read_document(row.body_hash)
    except (OSError, ValueError, EOFError):
        return _ContractRecheck(None, "旧合同关联候选缺少可核验的完整本地正文", False)
    if hashlib.sha256(text.encode("utf-8")).digest() != row.body_hash:
        return _ContractRecheck(None, "旧合同关联候选缺少可核验的完整本地正文", False)
    if row.published_at > observed_at:
        return _ContractRecheck(None, "旧合同关联候选披露时点晚于本轮截止", False)
    document = CompanyDocument(
        Disclosure(row.source, row.source_key, (row.code,), row.title, row.published_at, row.url, row.record_hash), text, True
    )
    candidate = ExtractCompanyLinkCandidate(document, observed_at, event_policy)
    if candidate is None or candidate.positive is None or candidate.positive.event_type is not CompanyPositiveType.OPERATING_CONTRACT:
        return _ContractRecheck(None, "旧合同关联候选未通过当前主体规则核验", True)
    return _ContractRecheck(candidate, "", True)


__all__ = [
    "CompanyPublicationLinks",
    "ReadCompanyPublicationLinks",
    "PendingCompanyContractLinkCodes",
]
