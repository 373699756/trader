"""Collect the annual revenue and official report inputs needed by company facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING

from trader.download.application.company_failures import CompanyFailureCode
from trader.download.application.company_progress import CompanyCancelled, CompanyProgressState, ReportCompanyProgress
from trader.download.domain.company_annual_income import CompanyAnnualIncome, CompanyAnnualIncomeRequest
from trader.download.domain.company_evidence import CompanyInputValidationFailure, Disclosure
from trader.download.domain.company_financial_report import CompanyFinancialReportRequest
from trader.download.domain.download_failure import DownloadFailureContext

if TYPE_CHECKING:
    from trader.download.application.company_evidence import CompanyEvidenceDependencies


@dataclass(frozen=True, slots=True)
class CompanyReportInputPlan:
    observed_at: datetime
    annual_income: tuple[CompanyAnnualIncomeRequest, ...]
    financial_reports: tuple[CompanyFinancialReportRequest, ...]


def PlanCompanyReportInputs(
    dependencies: CompanyEvidenceDependencies,
    population: tuple[str, ...],
    observed_at: datetime,
    state: CompanyProgressState,
) -> CompanyReportInputPlan:
    """Freeze known local requirements at one collection checkpoint."""
    if CompanyCancelled(dependencies) or state.annual_income_source_stopped and state.financial_report_source_stopped:
        return CompanyReportInputPlan(observed_at, (), ())
    plan = dependencies.index.report_input_plan(population, observed_at)
    annual_income = () if CompanyCancelled(dependencies) or state.annual_income_source_stopped else tuple(
        request for request in plan.annual_income
        if request not in state.annual_income_attempts
    )
    financial_reports = () if CompanyCancelled(dependencies) or state.financial_report_source_stopped else tuple(
        request for request in plan.financial_reports
        if request not in state.financial_report_attempts
    )
    return CompanyReportInputPlan(plan.observed_at, annual_income, financial_reports)


def CollectCompanyReportInputs(
    dependencies: CompanyEvidenceDependencies,
    plan: CompanyReportInputPlan,
    state: CompanyProgressState,
) -> int:
    """Execute only the frozen requests; new needs wait for a later checkpoint."""
    changes = _annual_income(dependencies, plan, state)
    changes += _financial_reports(dependencies, plan, state)
    return changes


def _financial_reports(
    dependencies: CompanyEvidenceDependencies,
    plan: CompanyReportInputPlan,
    state: CompanyProgressState,
) -> int:
    if CompanyCancelled(dependencies) or state.financial_report_source_stopped:
        return 0
    observed_at = plan.observed_at
    changes = 0
    for request in plan.financial_reports:
        if request in state.financial_report_attempts:
            continue
        if CompanyCancelled(dependencies) or state.financial_report_source_stopped:
            break
        state.financial_report_attempts.add(request)
        component = "annual_income" if request.report_date.month == 12 else "financial"
        source_error: BaseException | None = None

        def fetch(request: CompanyFinancialReportRequest = request) -> tuple[Disclosure, ...]:
            nonlocal source_error
            try:
                return dependencies.disclosures.financial_reports(request, observed_at=observed_at)
            except (OSError, RuntimeError, ValueError) as exc:
                source_error = exc
                raise

        try:
            identity = f"financial_report:{request.code}:{request.report_date}"
            rows, fetched = state.operations.call_observed(
                "annual_income" if request.report_date.month == 12 else "directory", fetch,
                code=request.code, source=dependencies.disclosures.source, object_id=identity,
                component=component, endpoint=dependencies.disclosures.request_endpoint(component),
            )
            if CompanyCancelled(dependencies):
                break
            changed, committed = state.operations.call_observed(
                "collection_commit", partial(dependencies.index.commit_financial_reports, request, rows, observed_at),
                code=request.code, source=dependencies.disclosures.source, object_id=identity,
                component=component, endpoint="sqlite:company_collection",
            )
            changes += changed
            state.operations.result(fetched, "accepted" if rows else "empty", rows=len(rows), commit=committed,
                                    changed_rows=changed, component=component)
            state.pages += 1
            state.financial_report_circuit.success()
        except (OSError, RuntimeError, ValueError) as exc:
            if CompanyCancelled(dependencies):
                break
            if exc is not source_error and not isinstance(exc, CompanyInputValidationFailure):
                raise
            failure = CompanyFailureCode(exc)
            state.failures += (f"{component}:{failure}",)
            state.financial_report_source_stopped = state.financial_report_circuit.failed(exc, validation=isinstance(exc, ValueError))
            state.operations.recover(exc, "source_deferred", failure,
                                    fallback=DownloadFailureContext("company_financial_report", supplier=dependencies.disclosures.source))
    return changes


def _annual_income(
    dependencies: CompanyEvidenceDependencies,
    plan: CompanyReportInputPlan,
    state: CompanyProgressState,
) -> int:
    if CompanyCancelled(dependencies) or state.annual_income_source_stopped:
        return 0
    observed_at = plan.observed_at
    requests = tuple(
        request for request in plan.annual_income
        if request not in state.annual_income_attempts
    )
    changes = 0
    for completed, request in enumerate(requests):
        if CompanyCancelled(dependencies) or state.annual_income_source_stopped:
            break
        state.current_stock = None
        ReportCompanyProgress(dependencies, "annual_income", state, len(requests) - completed, completed=completed)
        if CompanyCancelled(dependencies):
            break
        state.annual_income_attempts.add(request)
        source_error: BaseException | None = None

        def fetch(request: CompanyAnnualIncomeRequest = request) -> tuple[CompanyAnnualIncome, ...]:
            nonlocal source_error
            try:
                return dependencies.structured.annual_income(request, observed_at=observed_at)
            except (OSError, RuntimeError, ValueError) as exc:
                source_error = exc
                raise

        try:
            identity = f"annual_income:{request.code}:" + ",".join(day.isoformat() for day in request.report_dates)
            incomes, fetched = state.operations.call_observed(
                "annual_income", fetch,
                code=request.code, source=dependencies.structured.source, object_id=identity,
                component="annual_income", endpoint=dependencies.structured.request_endpoint("annual_income"),
            )
            if CompanyCancelled(dependencies):
                break
            changed, committed = state.operations.call_observed(
                "collection_commit", partial(dependencies.index.commit_annual_income, request, incomes, observed_at),
                code=request.code, source=dependencies.structured.source, object_id=identity,
                component="annual_income", endpoint="sqlite:company_collection",
            )
            changes += changed
            state.operations.result(fetched, "accepted" if incomes else "empty", rows=len(incomes),
                                    commit=committed, changed_rows=changed, component="annual_income")
            state.pages += 1
            state.annual_income_circuit.success()
            remaining = len(requests) - completed - 1
            ReportCompanyProgress(dependencies, "annual_income", state, remaining, completed=completed + 1,
                    status="running" if remaining else "complete")
        except (OSError, RuntimeError, ValueError) as exc:
            if CompanyCancelled(dependencies):
                break
            if exc is not source_error and not isinstance(exc, CompanyInputValidationFailure):
                raise
            failure = CompanyFailureCode(exc)
            state.failures += (f"annual_income:{failure}",)
            state.annual_income_source_stopped = state.annual_income_circuit.failed(exc, validation=isinstance(exc, ValueError))
            state.operations.recover(exc, "source_deferred", failure,
                                     fallback=DownloadFailureContext("company_annual_income", supplier=dependencies.structured.source))
            ReportCompanyProgress(dependencies, "annual_income", state, len(requests) - completed, completed=completed, status="deferred")
    return changes


__all__ = [
    "CompanyReportInputPlan",
    "PlanCompanyReportInputs",
    "CollectCompanyReportInputs",
]
