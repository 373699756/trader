"""Local proof for contract link candidates, independent of formal fact identity."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from trader.download.domain.company_event_extraction import CompanyLinkCandidate
from trader.download.domain.company_events import CompanyPositiveType
from trader.download.domain.company_evidence import Disclosure
from trader.download.infra.company_link_codec import EncodeCompanyLink
from trader.infra.sqlite_connection import SqliteConnection

_RULE = "contract_link_subject:1"


@dataclass(frozen=True, slots=True)
class CompanyContractLinkRule:
    source: str
    source_key: str
    code: str
    record_hash: str
    body_hash: bytes
    extraction_hash: bytes
    payload_hash: bytes


@dataclass(frozen=True, slots=True)
class CompanyContractLinkRefresh:
    binding: CompanyContractLinkRule
    candidate: CompanyLinkCandidate | None


def ReadCompanyContractLinkRules(
    connection: SqliteConnection, codes: Sequence[str], extraction_hash: bytes
) -> frozenset[CompanyContractLinkRule]:
    """Read current version-bound proof from an admitted collection."""
    if not codes:
        return frozenset()
    placeholders = ",".join("?" for _code in codes)
    return frozenset(CompanyContractLinkRule(*row) for row in connection.run(
        "SELECT source,source_key,code,record_hash,body_hash,extraction_hash,payload_hash "
        f"FROM document_contract_link_rules WHERE code IN ({placeholders}) AND extraction_hash=? AND rule_id=?",
        (*codes, extraction_hash, _RULE),
    ))


def WriteCompanyContractLinkRules(
    connection: SqliteConnection,
    disclosure: Disclosure,
    body_hash: bytes,
    extraction_hash: bytes,
    candidate: CompanyLinkCandidate | None,
) -> None:
    """Record a candidate just produced by the current subject guard.

    The caller writes the accepted body and candidate first, in this same
    transaction. Rejected/late results cannot bless another version or code.
    """
    if candidate is None or candidate.positive is None or (
        candidate.positive.event_type is not CompanyPositiveType.OPERATING_CONTRACT
    ):
        connection.run(
            "DELETE FROM document_contract_link_rules WHERE source=? AND source_key=? AND record_hash=? AND extraction_hash=?",
            (disclosure.source, disclosure.identity, disclosure.digest, extraction_hash),
        )
        return
    if len(body_hash) != 32 or candidate.evidence_id != disclosure.identity or candidate.published_at != disclosure.published_at:
        raise ValueError("company_contract_link_identity_invalid")
    payload = EncodeCompanyLink(candidate)
    codes = tuple(row[0] for row in connection.run(
        "SELECT c.code FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
        "JOIN document_link_candidates l ON l.source=d.source AND l.source_key=d.source_key "
        "AND l.record_hash=d.record_hash WHERE d.source=? AND d.source_key=? AND d.record_hash=? "
        "AND d.body_hash=? AND d.body_checked=1 AND l.extraction_hash=? AND l.payload=? ORDER BY c.code",
        (disclosure.source, disclosure.identity, disclosure.digest, body_hash, extraction_hash, payload),
    ))
    if not codes or not set(disclosure.codes).issubset(codes):
        return
    payload_hash = hashlib.sha256(payload.encode("utf-8")).digest()
    connection.run_many(
        "INSERT OR REPLACE INTO document_contract_link_rules VALUES(?,?,?,?,?,?,?,?)",
        (
            (disclosure.source, disclosure.identity, code, disclosure.digest, body_hash, extraction_hash, payload_hash, _RULE)
            for code in disclosure.codes
        ),
    )


def ApplyCompanyContractLinkRefreshes(
    connection: SqliteConnection, refreshes: Sequence[CompanyContractLinkRefresh]
) -> None:
    """Acknowledge local revalidation only while all current bindings still match."""
    if not refreshes:
        return
    for refresh in refreshes:
        binding = refresh.binding
        row = connection.run(
            "SELECT d.title,d.published_at,d.url,l.payload FROM disclosures d "
            "JOIN disclosure_codes c ON c.disclosure_id=d.id "
            "JOIN document_link_candidates l ON l.source=d.source AND l.source_key=d.source_key AND l.record_hash=d.record_hash "
            "WHERE d.source=? AND d.source_key=? AND c.code=? AND d.record_hash=? AND d.body_hash=? "
            "AND d.body_checked=1 AND l.extraction_hash=?",
            (binding.source, binding.source_key, binding.code, binding.record_hash, binding.body_hash, binding.extraction_hash),
        ).fetchone()
        if row is None:
            continue
        candidate = refresh.candidate
        new_payload = EncodeCompanyLink(candidate) if candidate is not None else None
        current_payload_hash = hashlib.sha256(row[3].encode("utf-8")).digest()
        if current_payload_hash != binding.payload_hash and (
            new_payload is None or current_payload_hash != hashlib.sha256(new_payload.encode("utf-8")).digest()
        ):
            continue
        if candidate is None:
            connection.run(
                "DELETE FROM document_link_candidates WHERE source=? AND source_key=? AND record_hash=? AND extraction_hash=?",
                (binding.source, binding.source_key, binding.record_hash, binding.extraction_hash),
            )
            connection.run(
                "DELETE FROM document_contract_link_rules WHERE source=? AND source_key=? AND record_hash=? AND extraction_hash=?",
                (binding.source, binding.source_key, binding.record_hash, binding.extraction_hash),
            )
            continue
        connection.run(
            "UPDATE document_link_candidates SET payload=? WHERE source=? AND source_key=? AND record_hash=? AND extraction_hash=?",
            (new_payload, binding.source, binding.source_key, binding.record_hash, binding.extraction_hash),
        )
        WriteCompanyContractLinkRules(
            connection,
            Disclosure(binding.source, binding.source_key, (binding.code,), row[0], datetime.fromisoformat(row[1]), row[2], binding.record_hash),
            binding.body_hash,
            binding.extraction_hash,
            candidate,
        )


__all__ = [
    "CompanyContractLinkRule",
    "CompanyContractLinkRefresh",
    "ReadCompanyContractLinkRules",
    "WriteCompanyContractLinkRules",
    "ApplyCompanyContractLinkRefreshes",
]
