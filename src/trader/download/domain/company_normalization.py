"""Pure financial selection; receiving a source row never proves an issuer revision."""

from dataclasses import dataclass, replace
from datetime import datetime

from trader.download.domain.company_evidence import CompanyInputValidationFailure
from trader.download.domain.company_events import CompanyDataState
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_source_records import CompanyFinancialSourceObservation, CompanyFinancialSourceValues


@dataclass(frozen=True, slots=True)
class CompanyNormalizationCommit:
    scope: str
    generation: bytes

    def __post_init__(self) -> None:
        if len(self.scope) != 64 or len(self.generation) != 32:
            raise ValueError("company_normalization_commit_invalid")


def NormalizeCompanyFinancialFacts(
    previous: CompanyFacts,
    observations: tuple[CompanyFinancialSourceObservation, ...],
    cutoff: datetime,
) -> CompanyFacts:
    """Retain every other component and reject ambiguous changes to a known report."""
    if cutoff.utcoffset() is None or previous.published_at > cutoff:
        raise ValueError("company_normalization_cutoff_invalid")
    if not observations:
        return previous
    known = tuple(item for item in (previous.financial, previous.latest_financial) if item is not None)
    received = []
    keys = set()
    for observation in observations:
        report = observation.financial
        if report.code != previous.code or observation.observed_at > cutoff:
            raise CompanyInputValidationFailure("company_normalization_observation_mismatch")
        if observation.business_key in keys:
            raise CompanyInputValidationFailure("company_normalization_duplicate_report")
        keys.add(observation.business_key)
        for original in known:
            if report.report_date == original.report_date and report != original:
                raise CompanyInputValidationFailure("company_normalization_revision_unproven")
        received.append(report)
    reports = (*known, *received)
    if not reports:
        return previous
    latest = max(reports, key=lambda item: (item.report_date, item.published_at))
    complete = tuple(item for item in reports if all(value is not None for _, value in CompanyFinancialSourceValues(item)))
    financial = max(complete, key=lambda item: (item.report_date, item.published_at)) if complete else latest
    state = CompanyDataState.COMPLETE if latest == financial and latest in complete else CompanyDataState.PARTIAL
    if (financial, latest, state) == (previous.financial, previous.latest_financial, previous.financial_state):
        return previous
    return replace(previous, financial=financial, latest_financial=latest, financial_state=state, published_at=cutoff)


__all__ = ["CompanyNormalizationCommit", "NormalizeCompanyFinancialFacts"]
