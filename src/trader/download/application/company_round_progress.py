"""Published field totals and report scope for company synchronization."""

from dataclasses import dataclass

from trader.download.domain.company_sync import CompanySyncFamily, CompanySyncFamilyResult


@dataclass(frozen=True, slots=True)
class CompanyFieldOutcomeCounts:
    """Totals from a single verified formal publication."""

    publication_hash: str
    verified: int
    present_unverified: int
    other_pending: int
    not_disclosed: int
    not_applicable: int
    pending_documents: int


def IsCompanyNewsReference(family: CompanySyncFamilyResult) -> bool:
    """Recognize a finished news lookup with only its ordinary scope marker."""
    step = family.result
    return (
        family.family is CompanySyncFamily.NEWS
        and step.processed_scope_complete
        and step.pending_count == 0
        and step.failure is None
        and step.gaps == ("news_metadata_does_not_verify_issuer_risk",)
    )


__all__ = [
    "CompanyFieldOutcomeCounts",
    "IsCompanyNewsReference",
]
