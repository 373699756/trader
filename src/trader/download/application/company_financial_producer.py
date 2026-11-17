"""One durable coordinator step per financial page, then pure publication."""

from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Literal, Protocol

from trader.download.application.company_source_ports import CompanyFinancialObservationSource
from trader.download.domain.company_evidence import StructuredCompanyQuery
from trader.download.domain.company_source_records import CompanyFinancialSourcePage
from trader.download.domain.company_sync import (
    CompanySyncFamily, CompanySyncInterrupted, CompanySyncPhasePlan, CompanySyncPhaseState, CompanySyncRoundScope, CompanySyncStepResult,
    CompanyFinancialRequestIdentity, CompanyFinancialCursorIdentity,
)


class CompanyFinancialInputIndex(Protocol):
    def cursor(self, query: StructuredCompanyQuery, codes: tuple[str, ...], cutoff: datetime) -> StructuredCompanyQuery | None: ...
    def accept(self, query: StructuredCompanyQuery, page: CompanyFinancialSourcePage, codes: tuple[str, ...], cutoff: datetime, *, eligible_codes: tuple[str, ...] | None = None) -> None: ...
    def normalize(self, query: StructuredCompanyQuery, codes: tuple[str, ...], cutoff: datetime) -> tuple[str, ...]: ...
    def publish_normalized(self, query: StructuredCompanyQuery, codes: tuple[str, ...], cutoff: datetime) -> tuple[str, ...]: ...


class CompanyFinancialProducer:
    def __init__(
        self, index: CompanyFinancialInputIndex, source: CompanyFinancialObservationSource, query: StructuredCompanyQuery, *,
        qualification_gaps: tuple[str, ...],
        publication_scope: Literal["observations", "isolated_candidate"] = "observations",
        current_codes: tuple[str, ...] | None = None,
    ) -> None:
        if query.component != "financial" or query.page != 1:
            raise ValueError("company_financial_producer_query_invalid")
        if publication_scope not in {"observations", "isolated_candidate"}:
            raise ValueError("company_financial_publication_scope_invalid")
        self._index, self._source, self._query, self._publication_scope = index, source, query, publication_scope
        self._current_codes = current_codes
        self._plan = CompanySyncPhasePlan(CompanySyncFamily.FINANCIAL, CompanyFinancialRequestIdentity(query, publication_scope), qualification_gaps)

    @property
    def plan(self) -> CompanySyncPhasePlan:
        return self._plan

    def state(self, scope: CompanySyncRoundScope) -> CompanySyncPhaseState:
        today = scope.cutoff.astimezone(ZoneInfo("Asia/Shanghai")).date()
        if self._query.start < today - timedelta(days=365) or self._query.end > today:
            raise ValueError("company_round_window_invalid")
        cursor = self._index.cursor(self._query, scope.codes, scope.cutoff)
        # Only the exact persisted page cursor reconciles a prior request.
        return CompanySyncPhaseState(CompanyFinancialCursorIdentity(self._query, cursor), cursor is not None and self._current_codes != ())

    def advance(self, scope: CompanySyncRoundScope, *, cancel_requested: Callable[[], bool], deadline: float) -> CompanySyncStepResult:
        if self._current_codes == ():
            return CompanySyncStepResult(True, False, gaps=("population_members_became_ineligible",), pending_count=None)
        cursor = self._index.cursor(self._query, scope.codes, scope.cutoff)
        if cursor is not None:
            page = self._source.financial_observations(cursor, observed_at=scope.cutoff)
            # Accept even when a deadline arrives after the response. If the
            # material owner refuses cancellation, the intent stays unknown.
            try:
                self._index.accept(cursor, page, scope.codes, scope.cutoff, eligible_codes=self._current_codes)
            except InterruptedError as exc:
                if str(exc) != "company_round_cancelled" or not cancel_requested():
                    raise
                raise CompanySyncInterrupted("company_financial_accept_cancelled") from exc
            return CompanySyncStepResult(False, True, accepted_inputs=len(page.observations), source_calls=1)
        if self._publication_scope == "observations":
            return CompanySyncStepResult(True, False, pending_count=0)
        try:
            self._index.normalize(self._query, scope.codes, scope.cutoff)
        except ValueError as exc:
            if str(exc) == "company_normalization_field_rules_changed":
                return CompanySyncStepResult(False, True, gaps=("field_rules_revalidation_required",), deferred=True)
            if str(exc) != "company_normalization_revision_unproven":
                raise
            return CompanySyncStepResult(False, False, gaps=("issuer_revision_unproven",), failure="issuer_revision_unproven")
        changed = self._index.publish_normalized(self._query, scope.codes, scope.cutoff)
        return CompanySyncStepResult(True, False, changed_codes=changed, pending_count=0)


__all__ = ["CompanyFinancialInputIndex", "CompanyFinancialProducer"]
