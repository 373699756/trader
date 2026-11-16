"""Immutable decoded inputs accepted by the formal company writer."""

from dataclasses import dataclass
from datetime import datetime

from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_field_results import CompanyFieldConclusion
from trader.download.infra.company_contract_link_rules import CompanyContractLinkRefresh
from trader.download.infra.company_facts_index import CompanyInputReceipt, CompanyPublicationIdentity
from trader.download.infra.company_field_attempts import CompanyFieldAttempt


@dataclass(frozen=True, slots=True)
class CompanyDocumentPublication:
    disclosure_id: int
    code: str
    record_hash: str
    extraction_hash: bytes
    published_at: datetime


@dataclass(frozen=True, slots=True)
class CompanyPublicationCandidate:
    codes: tuple[str, ...]
    observed_at: datetime
    parent: CompanyPublicationIdentity | None
    facts: tuple[CompanyFacts, ...]
    changed_facts: tuple[CompanyFacts, ...]
    sources: tuple[tuple[str, str], ...]
    receipts: tuple[CompanyInputReceipt, ...]
    document_receipts: tuple[CompanyDocumentPublication, ...]
    fields: tuple[CompanyFieldConclusion, ...]
    contract_refreshes: tuple[CompanyContractLinkRefresh, ...]
    field_attempts: tuple[CompanyFieldAttempt, ...]


__all__ = ["CompanyDocumentPublication", "CompanyPublicationCandidate"]
