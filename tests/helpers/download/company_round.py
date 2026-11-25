"""Explicit round outcomes for orchestration tests; never source-admission evidence."""

from datetime import datetime

from trader.download.domain.company_sync import (
    COMPANY_SYNC_FAMILIES, CompanySyncFamily, CompanySyncFamilyResult, CompanySyncRoundResult, CompanySyncRoundScope, CompanySyncStepResult,
)


def CompanySyncFamilyFixture(
    family: CompanySyncFamily, *, complete: bool = True, can_continue: bool = False,
    pending_count: int | None = 0, gaps: tuple[str, ...] = (), failure: str | None = None,
    accepted_inputs: int = 0, source_calls: int | None = 0,
) -> CompanySyncFamilyResult:
    return CompanySyncFamilyResult(family, 1, accepted_inputs, source_calls, CompanySyncStepResult(
        complete, can_continue, accepted_inputs, 0, (), gaps, pending_count, failure,
    ))


def CompanySyncRoundFixture(
    observed_at: datetime, codes: tuple[str, ...] = ("600001",), *,
    families: tuple[CompanySyncFamilyResult, ...] | None = None,
    cancelled: bool = False, budget_exhausted: bool = False,
) -> CompanySyncRoundResult:
    return CompanySyncRoundResult(
        round_id="a" * 64, scope=CompanySyncRoundScope(codes, observed_at),
        families=families if families is not None else tuple(CompanySyncFamilyFixture(family) for family in COMPANY_SYNC_FAMILIES),
        cancelled=cancelled, budget_exhausted=budget_exhausted, publication_hash="b" * 64,
    )
