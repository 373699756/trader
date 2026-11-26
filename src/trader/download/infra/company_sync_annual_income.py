"""Current contract dependencies share the fixed source budget and durable annual attempts."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from trader.download.application.company_failures import CompanyFailureCode, IsRetryableCompanySourceFailure
from trader.download.application.company_source_ports import CompanyAnnualIncomeSource
from trader.download.domain.company_annual_income import CompanyAnnualIncomeRequest, PlanCompanyAnnualIncome
from trader.download.domain.company_evidence import CompanyInputValidationFailure, CompanySourceFailure
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_sync import (
    CompanyAnnualIncomeDownloadProgress, CompanyDownloadIssue, CompanyDownloadOperation, CompanySourceDownloadProgress,
    CompanySyncFamily, CompanySyncPhasePlan, CompanySyncPhaseState, CompanySyncRoundScope, CompanySyncStepResult,
    CompanySyncInterrupted,
)
from trader.download.domain.download_failure import DownloadValidationFailure
from trader.download.infra.company_sync_inputs import SQLiteCompanySyncInputStore
from trader.infra.serialization.canonical_codec import CanonicalDigest


@dataclass(frozen=True, slots=True)
class _AnnualDemand:
    request: CompanyAnnualIncomeRequest | None
    facts: tuple[CompanyFacts, ...]
    pending_count: int
    gaps: tuple[str, ...]


class CompanyAnnualIncomeProducer:
    def __init__(
        self,
        inputs: SQLiteCompanySyncInputStore,
        source: CompanyAnnualIncomeSource,
        read_facts: Callable[[], tuple[CompanyFacts, ...]],
        *,
        current_codes: tuple[str, ...],
        observe_download: Callable[[CompanySourceDownloadProgress], None] | None = None,
    ) -> None:
        self._inputs, self._source, self._read_facts = inputs, source, read_facts
        self._current_codes = current_codes
        self._observe_download = observe_download
        self._plan = CompanySyncPhasePlan(CompanySyncFamily.ANNUAL_INCOME,
            CanonicalDigest(("Eastmoney.F10.NewFinanceAnalysis", "OPERATE_INCOME", 365, 5)), refresh_on_input_change=True)

    @property
    def plan(self) -> CompanySyncPhasePlan:
        return self._plan

    def _demand(self, scope: CompanySyncRoundScope) -> _AnnualDemand:
        frozen = self._inputs.pending_annual_request(scope.identity)
        if frozen is not None:
            # An unconfirmed request keeps its exact identity even if another
            # family publishes a new contract. The journal decides recovery.
            return _AnnualDemand(frozen, (), 0, ())
        facts = self._read_facts()
        requests: list[CompanyAnnualIncomeRequest] = []
        pending = 0
        gaps: set[str] = set()
        for fact in facts:
            if fact.code not in scope.codes or fact.code not in self._current_codes:
                continue
            financials = tuple(row for row in (fact.financial, fact.latest_financial) if row is not None)
            plan = PlanCompanyAnnualIncome(fact.code, fact.positive_events, scope.cutoff, financials=financials)
            pending += plan.pending_count
            gaps.update(plan.gaps)
            if not plan.report_dates:
                continue
            attempted = self._inputs.attempted_annual_periods(scope.identity, fact.code)
            dates: tuple[date, ...] = tuple(day for day in plan.report_dates if day not in attempted)
            if dates:
                requests.append(CompanyAnnualIncomeRequest(fact.code, dates[:5]))
        if pending and not requests:
            gaps.add("annual_income_value_or_version_unverified")
        return _AnnualDemand(requests[0] if requests else None, facts, pending, tuple(sorted(gaps)))

    def state(self, scope: CompanySyncRoundScope) -> CompanySyncPhaseState:
        demand = self._demand(scope)
        request = demand.request
        return CompanySyncPhaseState(CanonicalDigest(request) if request is not None else CanonicalDigest(demand),
            request is not None and request.code in self._current_codes)

    def advance(self, scope: CompanySyncRoundScope, *, cancel_requested: Callable[[], bool], deadline: float) -> CompanySyncStepResult:
        demand = self._demand(scope)
        request = demand.request
        if request is None:
            failures = self._inputs.annual_failures(scope.identity)
            gaps = demand.gaps + (("annual_income_source_failed",) if failures else ())
            return CompanySyncStepResult(True, False, pending_count=demand.pending_count,
                gaps=gaps, failure=failures[0] if failures else None)
        if request.code not in self._current_codes:
            self._inputs.reject_annual(scope.identity, request, "population_members_became_ineligible")
            return CompanySyncStepResult(False, True, gaps=("population_members_became_ineligible",))
        if cancel_requested():
            raise CompanySyncInterrupted("company_annual_income_cancelled_before_source")
        self._inputs.begin_annual_request(scope.identity, request)
        operation = CompanyDownloadOperation(self._plan.family, "annual_income", request)
        if self._observe_download is not None:
            self._observe_download(operation)
        try:
            rows = self._source.annual_income(request, observed_at=scope.cutoff)
            if (any(item.code != request.code or item.observed_at != scope.cutoff for item in rows)
                or tuple(sorted(item.report_date for item in rows)) != request.report_dates):
                raise CompanyInputValidationFailure("company_annual_income_response_scope_mismatch")
        except (CompanyInputValidationFailure, CompanySourceFailure, DownloadValidationFailure) as exc:
            failure = CompanyFailureCode(exc)
            if isinstance(exc, CompanySourceFailure) and IsRetryableCompanySourceFailure(failure):
                raise
            self._inputs.reject_annual(scope.identity, request, failure)
            if self._observe_download is not None:
                self._observe_download(CompanyDownloadIssue(operation, failure, material_accepted=False, action="defer"))
            return CompanySyncStepResult(False, True, source_calls=1, gaps=("annual_income_source_failed",), failure=failure)
        if cancel_requested():
            raise CompanySyncInterrupted("company_annual_income_cancelled_after_source")
        self._inputs.accept_annual(scope.identity, request, rows, scope.cutoff)
        if self._observe_download is not None:
            self._observe_download(CompanyAnnualIncomeDownloadProgress(request, len(rows)))
        return CompanySyncStepResult(False, True, accepted_inputs=len(rows), source_calls=1)


__all__ = ["CompanyAnnualIncomeProducer"]
