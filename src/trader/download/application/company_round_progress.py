"""Published field totals and report scope for company synchronization."""

from dataclasses import dataclass

from trader.download.domain.company_source_records import CompanyFinancialField
from trader.download.domain.company_sync import CompanySyncFamily, CompanySyncFamilyResult


@dataclass(frozen=True, slots=True)
class CompanyEventFieldCoverage:
    """Coverage of measurement fields for published events in the round scope."""

    verified: int
    present_unverified: int
    pending: int
    not_disclosed: int
    not_applicable: int
    missing_values: int = 0

    @property
    def applicable(self) -> int:
        return self.verified + self.present_unverified + self.pending + self.not_disclosed


@dataclass(frozen=True, slots=True)
class CompanyFormalCoverage:
    """Verified company facts within one fixed synchronization population."""

    stocks: int
    financial_complete: int
    latest_profit_values: int
    pledge_with_value: int
    pledge_without_event: int
    unlock_with_event: int
    unlock_without_event: int
    announcement_complete: int
    announcement_partial: int
    verified_risk_stocks: int
    financial_fields: tuple[tuple[CompanyFinancialField, int], ...]
    other_fields: CompanyEventFieldCoverage
    reduction_fields: CompanyEventFieldCoverage

    @property
    def recommendation_expected(self) -> int:
        """Eight financial columns plus pledge, unlock and announcement coverage per stock."""
        return self.stocks * (len(self.financial_fields) + 3)

    @property
    def recommendation_missing(self) -> int:
        covered = (
            sum(count for _, count in self.financial_fields)
            + self.pledge_with_value + self.pledge_without_event
            + self.unlock_with_event + self.unlock_without_event
            + self.announcement_complete
        )
        return self.recommendation_expected - covered


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
    coverage: CompanyFormalCoverage | None = None


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
    "CompanyEventFieldCoverage",
    "CompanyFieldOutcomeCounts",
    "CompanyFormalCoverage",
    "IsCompanyNewsReference",
]
