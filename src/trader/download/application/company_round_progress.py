"""Published field totals and report scope for company synchronization."""

from dataclasses import dataclass

from trader.download.domain.company_field_results import CompanyFieldName
from trader.download.domain.company_source_records import CompanyFinancialField


@dataclass(frozen=True, slots=True)
class CompanyRequestProgress:
    """A committed request-ledger snapshot for one fixed company round."""

    round_id: str
    limit: int | None
    used: int
    unknown: int


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


def _sum_event_fields(
    fields: tuple[tuple[CompanyFieldName, CompanyEventFieldCoverage], ...],
) -> CompanyEventFieldCoverage:
    return CompanyEventFieldCoverage(
        verified=sum(counts.verified for _, counts in fields),
        present_unverified=sum(counts.present_unverified for _, counts in fields),
        pending=sum(counts.pending for _, counts in fields),
        not_disclosed=sum(counts.not_disclosed for _, counts in fields),
        not_applicable=sum(counts.not_applicable for _, counts in fields),
        missing_values=sum(counts.missing_values for _, counts in fields),
    )


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
    other_field_counts: tuple[tuple[CompanyFieldName, CompanyEventFieldCoverage], ...]
    reduction_field_counts: tuple[tuple[CompanyFieldName, CompanyEventFieldCoverage], ...]
    financial_scoring_stocks: int | None = None
    unlock_partial_with_event: int = 0

    @property
    def other_fields(self) -> CompanyEventFieldCoverage:
        return _sum_event_fields(self.other_field_counts)

    @property
    def reduction_fields(self) -> CompanyEventFieldCoverage:
        return _sum_event_fields(self.reduction_field_counts)

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


__all__ = [
    "CompanyEventFieldCoverage",
    "CompanyFieldOutcomeCounts",
    "CompanyFormalCoverage",
    "CompanyRequestProgress",
]
