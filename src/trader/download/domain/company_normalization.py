"""Pure financial selection; receiving a source row never proves an issuer revision."""

from dataclasses import dataclass, replace
from datetime import datetime

from trader.download.domain.company_evidence import CompanyFinancial, CompanyInputValidationFailure
from trader.download.domain.company_events import CompanyDataState
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_financial_admission import CompanyFinancialAdmission
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
    return _select_financial_reports(previous, (*known, *received), cutoff)


def ApplyCompanyFinancialAdmission(
    previous: CompanyFacts,
    proof: CompanyFinancialAdmission,
    cutoff: datetime,
) -> CompanyFacts:
    """Keep the complete scoring report alongside the latest verified report."""
    report = proof.report
    if (cutoff.utcoffset() is None or previous.published_at > cutoff or report.code != previous.code
        or report.published_at > cutoff or proof.revision.checked_through != cutoff):
        raise ValueError("company_financial_admission_scope_invalid")
    known = tuple(item for item in (previous.financial, previous.latest_financial) if item is not None)
    for original in known:
        if original.report_date != report.report_date:
            continue
        if original.published_at != report.published_at:
            return previous
        values = []
        for (_, value), (_, old) in zip(CompanyFinancialSourceValues(report), CompanyFinancialSourceValues(original), strict=True):
            if value is not None and old is not None and value != old:
                return previous
            values.append(old if value is None else value)
        report = CompanyFinancial(report.code, report.report_date, report.published_at, *values)
    other_reports = tuple(item for item in known if item.report_date != report.report_date)
    return _select_financial_reports(previous, (*other_reports, report), cutoff)


def _select_financial_reports(
    previous: CompanyFacts,
    reports: tuple[CompanyFinancial, ...],
    cutoff: datetime,
) -> CompanyFacts:
    if not reports:
        return previous
    latest = max(reports, key=lambda item: (item.report_date, item.published_at))
    complete = tuple(item for item in reports if all(value is not None for _, value in CompanyFinancialSourceValues(item)))
    financial = max(complete, key=lambda item: (item.report_date, item.published_at)) if complete else latest
    state = CompanyDataState.COMPLETE if latest == financial and latest in complete else CompanyDataState.PARTIAL
    if (financial, latest, state) == (previous.financial, previous.latest_financial, previous.financial_state):
        return previous
    return replace(previous, financial=financial, latest_financial=latest, financial_state=state, published_at=cutoff)


__all__ = ["ApplyCompanyFinancialAdmission", "CompanyNormalizationCommit", "NormalizeCompanyFinancialFacts"]
