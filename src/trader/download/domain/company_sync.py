"""Finite company processing and business completeness are different conclusions."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from math import isfinite
from zoneinfo import ZoneInfo

from trader.download.domain.company_evidence import EvidenceMarket, StructuredCompanyQuery
from trader.infra.serialization.canonical_codec import CanonicalDigest


class CompanySyncFamily(str,Enum):
    FINANCIAL="financial"
    PLEDGE="pledge"
    UNLOCK="unlock"
    ANNOUNCEMENTS="announcements"
    ANNUAL_INCOME="annual_income"
    NEWS="news"
    FIELDS="fields"


@dataclass(frozen=True,slots=True)
class CompanySyncRoundScope:
    codes:tuple[str,...]
    cutoff:datetime
    families:tuple[CompanySyncFamily,...]=tuple(CompanySyncFamily)

    def __post_init__(self)->None:
        if (not self.codes or len(self.codes)>12000 or self.codes!=tuple(sorted(set(self.codes))) or self.cutoff.utcoffset() is None
            or not self.families or self.families!=tuple(family for family in CompanySyncFamily if family in self.families)):
            raise ValueError("company_sync_scope_invalid")
        for code in self.codes:
            if not code.isascii() or len(code)!=6 or not code.isdigit():
                raise ValueError("company_sync_scope_invalid")
            EvidenceMarket(code)
        object.__setattr__(self,"cutoff",self.cutoff.astimezone(ZoneInfo("Asia/Shanghai")))

    @property
    def identity(self)->str:
        return CanonicalDigest((self.codes,self.cutoff,self.families))


@dataclass(frozen=True,slots=True)
class CompanySyncPhasePlan:
    family:CompanySyncFamily
    request_hash:str
    qualification_gaps:tuple[str,...]=()
    refresh_on_input_change:bool=False

    def __post_init__(self)->None:
        if len(self.request_hash)!=64 or any(char not in "0123456789abcdef" for char in self.request_hash) or type(self.refresh_on_input_change) is not bool:
            raise ValueError("company_sync_plan_identity_invalid")
        _gaps(self.qualification_gaps)


@dataclass(frozen=True,slots=True)
class CompanySyncBudget:
    max_steps:int
    max_source_calls:int
    steps_per_family:int
    deadline:float

    def __post_init__(self)->None:
        if (any(type(value) is not int for value in (self.max_steps,self.max_source_calls,self.steps_per_family))
            or not 1<=self.max_steps<=4096 or not 0<=self.max_source_calls<=2048
            or not 1<=self.steps_per_family<=256 or not isfinite(self.deadline)):
            raise ValueError("company_sync_budget_invalid")


@dataclass(frozen=True,slots=True)
class CompanySyncPhaseState:
    identity:str
    source_required:bool
    local_input_verified:bool=False

    def __post_init__(self)->None:
        if (len(self.identity)!=64 or type(self.source_required) is not bool or type(self.local_input_verified) is not bool
            or self.source_required and self.local_input_verified):
            raise ValueError("company_sync_phase_state_invalid")


@dataclass(frozen=True,slots=True)
class CompanySyncStepResult:
    processed_scope_complete:bool
    can_continue:bool
    accepted_inputs:int|None=0
    source_calls:int=0
    changed_codes:tuple[str,...]=()
    gaps:tuple[str,...]=()
    pending_count:int|None=None
    failure:str|None=None
    deferred:bool=False

    def __post_init__(self)->None:
        if (any(type(flag) is not bool for flag in (self.processed_scope_complete,self.can_continue,self.deferred))
            or self.deferred and not self.can_continue
            or self.processed_scope_complete and self.can_continue
            or self.accepted_inputs is not None and (type(self.accepted_inputs) is not int or self.accepted_inputs<0)
            or type(self.source_calls) is not int or not 0<=self.source_calls<=2048
            or self.pending_count is not None and (type(self.pending_count) is not int or self.pending_count<0)
            or self.changed_codes!=tuple(sorted(set(self.changed_codes)))
            or self.failure is not None and (not self.failure or len(self.failure)>160)):
            raise ValueError("company_sync_step_result_invalid")
        _gaps(self.gaps)

    @property
    def business_complete(self)->bool:
        return self.processed_scope_complete and not self.gaps and self.pending_count==0 and self.failure is None


@dataclass(frozen=True,slots=True)
class CompanySyncFamilyResult:
    """source_calls counts reserved request slots, including unknown attempts."""
    family:CompanySyncFamily
    steps:int
    accepted_inputs:int|None
    source_calls:int|None
    result:CompanySyncStepResult
    unknown_attempts:int=0

    def __post_init__(self)->None:
        if (type(self.steps) is not int or self.steps < 0
            or self.accepted_inputs is not None and (type(self.accepted_inputs) is not int or self.accepted_inputs < 0)
            or self.source_calls is not None and (type(self.source_calls) is not int or self.source_calls < 0)
            or type(self.unknown_attempts) is not int or self.unknown_attempts < 0
            or self.source_calls is not None and self.unknown_attempts > self.source_calls):
            raise ValueError("company_sync_family_counts_invalid")


@dataclass(frozen=True,slots=True)
class CompanySyncRoundResult:
    round_id:str
    scope:CompanySyncRoundScope
    families:tuple[CompanySyncFamilyResult,...]
    cancelled:bool
    budget_exhausted:bool
    publication_hash:str

    def __post_init__(self)->None:
        families=tuple(item.family for item in self.families)
        if (families != tuple(family for family in CompanySyncFamily if family in families)
            or type(self.cancelled) is not bool or type(self.budget_exhausted) is not bool):
            raise ValueError("company_sync_round_result_invalid")

    @property
    def processing_complete(self)->bool:
        return (not self.cancelled and tuple(item.family for item in self.families)==self.scope.families
                and all(not item.result.can_continue for item in self.families))

    @property
    def business_complete(self)->bool:
        return self.scope.families==tuple(CompanySyncFamily) and self.processing_complete and all(item.result.business_complete for item in self.families)

    @property
    def needs_retry(self)->bool:
        return not self.business_complete

    @property
    def paused(self)->bool:
        return self.cancelled or self.budget_exhausted


@dataclass(frozen=True, slots=True)
class CompanySyncResume:
    round_id: str
    scope: CompanySyncRoundScope
    plans: tuple[CompanySyncPhasePlan, ...]
    max_source_calls: int
    source_calls: int


def CompanySyncRoundIdentity(scope: CompanySyncRoundScope, plans: tuple[CompanySyncPhasePlan, ...]) -> str:
    """Bind frozen source plans while allowing local proof rules to advance."""
    return CanonicalDigest((scope, tuple((plan.family, plan.request_hash) for plan in plans
        if not plan.refresh_on_input_change or plan.family is not CompanySyncFamily.FIELDS)))


def CompanyFinancialRequestIdentity(query: StructuredCompanyQuery, publication_scope: str) -> str:
    return CanonicalDigest((query, publication_scope))


def CompanyFinancialCursorIdentity(query: StructuredCompanyQuery, cursor: StructuredCompanyQuery | None) -> str:
    return CanonicalDigest((query, cursor))


class CompanySourceCancelled(OSError):
    """A source operation explicitly stopped before or during external I/O."""


class CompanySyncSourceBudgetExceeded(RuntimeError):
    """The fixed round has no remaining physical HTTP allowance."""


class CompanySyncInterrupted(InterruptedError):
    """An explicit coordinator cancellation/deadline check stopped this step."""


def _gaps(values:tuple[str,...])->None:
    if len(values)>64 or len(set(values))!=len(values) or any(not value or len(value)>160 for value in values):
        raise ValueError("company_sync_gaps_invalid")
