"""One finite full-company round; every family keeps its own truthful outcome."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from trader.download.domain.company_sync import (
    CompanySyncBudget,CompanySyncFamily,CompanySyncFamilyResult,CompanySyncPhasePlan,CompanySyncPhaseState,
    CompanySyncRoundResult,CompanySyncRoundScope,CompanySyncSourceBudgetExceeded,CompanySyncStepResult,CompanySyncInterrupted,CompanySourceCancelled,
)
from trader.download.domain.company_evidence import CompanyInputValidationFailure, CompanySourceFailure
from trader.download.domain.download_failure import DownloadValidationFailure
from trader.download.application.company_failures import CompanyFailureCode


class CompanySyncProducer(Protocol):
    @property
    def plan(self)->CompanySyncPhasePlan: ...
    def state(self,scope:CompanySyncRoundScope)->CompanySyncPhaseState: ...
    def advance(self,scope:CompanySyncRoundScope,*,cancel_requested:Callable[[],bool],deadline:float)->CompanySyncStepResult: ...


class CompanySyncRoundJournal(Protocol):
    def begin(self,scope:CompanySyncRoundScope,plans:tuple[CompanySyncPhasePlan,...],*,max_source_calls:int)->str: ...
    def source_calls(self,round_id:str)->int: ...
    def family(self,round_id:str,family:CompanySyncFamily)->CompanySyncFamilyResult|None: ...
    def outstanding(self,round_id:str,family:CompanySyncFamily)->CompanySyncPhaseState|None: ...
    def outstanding_calls(self,round_id:str,family:CompanySyncFamily)->int: ...
    def outstanding_family(self,round_id:str)->CompanySyncFamily|None: ...
    def completed_state(self,round_id:str,family:CompanySyncFamily)->CompanySyncPhaseState|None: ...
    def start(self,round_id:str,family:CompanySyncFamily,state:CompanySyncPhaseState)->None: ...
    def finish(self,round_id:str,family:CompanySyncFamily,result:CompanySyncStepResult)->None: ...
    def publication_identity(self)->str: ...


@dataclass
class _FamilyProgress:
    producer: CompanySyncProducer
    steps: int = 0
    result: CompanySyncFamilyResult | None = None
    deferred: bool = False


def RunCompanySyncRound(
    journal:CompanySyncRoundJournal,
    producers:tuple[CompanySyncProducer,...],
    scope:CompanySyncRoundScope,
    budget:CompanySyncBudget,
    *,
    monotonic:Callable[[],float],
    cancel_requested:Callable[[],bool],
    observe:Callable[[CompanySyncFamilyResult],None]|None=None,
)->CompanySyncRoundResult:
    """Run bounded steps, retain accepted work and never reissue an unconfirmed request.

    A family can finish its permitted work with qualification or quality gaps.
    Other local and independent families still run. No failed/empty attempt is
    retried within this fixed round merely because another family advanced.
    """
    if tuple(producer.plan.family for producer in producers)!=scope.families:
        raise ValueError("company_sync_producers_invalid")
    round_id=journal.begin(scope,tuple(producer.plan for producer in producers),max_source_calls=budget.max_source_calls)
    steps=0
    stopped=False
    exhausted=False
    pending_family=journal.outstanding_family(round_id)
    ordered_producers=tuple(producer for producer in producers if producer.plan.family==pending_family)+tuple(producer for producer in producers if producer.plan.family!=pending_family)
    progress=tuple(_FamilyProgress(producer) for producer in ordered_producers)
    while not stopped:
        advanced=False
        for item in progress:
            producer=item.producer
            family=producer.plan.family
            if item.deferred or item.steps>=budget.steps_per_family:
                continue
            if cancel_requested() or monotonic()>=budget.deadline:
                exhausted=not cancel_requested()
                stopped=True
                break
            current=journal.family(round_id,family)
            if current is not None:
                item.result=current
            refresh=(current is not None and producer.plan.refresh_on_input_change
                and journal.completed_state(round_id,family)!=producer.state(scope))
            if current is not None and not current.result.can_continue and not refresh:
                exhausted=exhausted or "source_budget_exhausted" in current.result.gaps
                continue
            if steps>=budget.max_steps:
                exhausted=True
                stopped=True
                break
            state=producer.state(scope)
            outstanding=journal.outstanding(round_id,family)
            reconciled=outstanding is not None and (outstanding.source_required or outstanding.identity!=state.identity)
            if reconciled and outstanding is not None:
                if outstanding.source_required and outstanding.identity==state.identity and not state.local_input_verified:
                    result=(CompanySyncStepResult(False,True,deferred=True)
                        if journal.outstanding_calls(round_id,family)==0 else
                        CompanySyncStepResult(False,False,gaps=("source_attempt_unconfirmed",),failure="source_attempt_unconfirmed"))
                else:
                    result=CompanySyncStepResult(False,True,accepted_inputs=None,gaps=("accepted_input_count_unconfirmed",))
            else:
                if outstanding is None:
                    journal.start(round_id,family,state)
                if state.source_required and journal.source_calls(round_id)>=budget.max_source_calls:
                    exhausted=True
                    result=CompanySyncStepResult(False,False,gaps=("source_budget_exhausted",))
                else:
                    try:
                        result=_invoke_step(producer,scope,budget,monotonic,cancel_requested)
                    except CompanySyncInterrupted:
                        item.result=_interrupted_family(item.result,producer.plan,state.source_required)
                        exhausted=not cancel_requested()
                        stopped=True
                        break
                    exhausted=exhausted or "source_budget_exhausted" in result.gaps
            steps+=1
            item.steps+=1
            advanced=True
            journal.finish(round_id,family,_with_plan_gaps(result,producer.plan))
            current=journal.family(round_id,family)
            if current is not None:
                item.result=current
                if observe is not None:
                    observe(current)
            if result.deferred and not reconciled:
                item.deferred=True
            if cancel_requested():
                stopped=True
                break
        if not advanced:
            break
    # Do not open another database after cancellation to manufacture a final
    # inventory or publication count. The last acknowledged head stays explicit.
    publication="" if cancel_requested() or monotonic()>=budget.deadline else journal.publication_identity()
    ordered_results=tuple(sorted((item.result for item in progress if item.result is not None),
        key=lambda result:tuple(CompanySyncFamily).index(result.family)))
    return CompanySyncRoundResult(round_id,scope,ordered_results,cancel_requested(),exhausted,publication)


def _with_plan_gaps(result:CompanySyncStepResult,plan:CompanySyncPhasePlan)->CompanySyncStepResult:
    gaps=plan.qualification_gaps+tuple(gap for gap in result.gaps if gap not in plan.qualification_gaps)
    return replace(result,gaps=gaps)


def _interrupted_family(
    previous:CompanySyncFamilyResult|None,plan:CompanySyncPhasePlan,source_required:bool,
)->CompanySyncFamilyResult:
    calls=0 if previous is None else previous.source_calls
    unknown=0 if previous is None else previous.unknown_attempts
    gaps=(() if previous is None else previous.result.gaps)+("step_interrupted",)
    if source_required:
        calls,unknown=None,None
        gaps+=("request_counts_unconfirmed",)
    result=CompanySyncStepResult(False,True,accepted_inputs=None,gaps=gaps,pending_count=None,
        changed_codes=() if previous is None else previous.result.changed_codes,
        failure=None if previous is None else previous.result.failure)
    return CompanySyncFamilyResult(plan.family,0 if previous is None else previous.steps,None,calls,
        _with_plan_gaps(result,plan),unknown)


def _invoke_step(producer:CompanySyncProducer,scope:CompanySyncRoundScope,budget:CompanySyncBudget,
    monotonic:Callable[[],float],cancel_requested:Callable[[],bool])->CompanySyncStepResult:
    try:
        return producer.advance(scope,cancel_requested=cancel_requested,deadline=budget.deadline)
    except CompanySyncSourceBudgetExceeded:
        return CompanySyncStepResult(False,False,gaps=("source_budget_exhausted",))
    except InterruptedError:
        raise
    except CompanySourceCancelled as exc:
        if not cancel_requested() and monotonic()<budget.deadline:
            raise
        raise CompanySyncInterrupted("company_sync_source_interrupted") from exc
    except (CompanyInputValidationFailure,CompanySourceFailure,DownloadValidationFailure) as exc:
        return CompanySyncStepResult(False,False,failure=CompanyFailureCode(exc),gaps=("supplier_input_rejected",))


def ResumeCompanySyncPendingStep(journal:CompanySyncRoundJournal,producers:tuple[CompanySyncProducer,...],
    scope:CompanySyncRoundScope,round_id:str,budget:CompanySyncBudget,*,monotonic:Callable[[],float],cancel_requested:Callable[[],bool],
    observe:Callable[[CompanySyncFamilyResult],None]|None=None)->bool:
    """Reconcile one old intent before a new local prerequisite; never start HTTP."""
    family=journal.outstanding_family(round_id)
    if family is None:
        return False
    if cancel_requested() or monotonic()>=budget.deadline:
        raise CompanySyncInterrupted("company_sync_recovery_interrupted")
    producer=next(item for item in producers if item.plan.family is family)
    old=journal.outstanding(round_id,family)
    if old is None:
        raise ValueError("company_sync_pending_owner_changed")
    current=producer.state(scope)
    if old.source_required and old.identity==current.identity and not current.local_input_verified:
        result=(CompanySyncStepResult(False,True,deferred=True) if journal.outstanding_calls(round_id,family)==0 else
            CompanySyncStepResult(False,False,gaps=("source_attempt_unconfirmed",),failure="source_attempt_unconfirmed"))
    elif old.identity!=current.identity:
        result=CompanySyncStepResult(False,True,accepted_inputs=None,gaps=("accepted_input_count_unconfirmed",))
    else:
        if current.source_required:
            raise ValueError("company_sync_recovery_source_state_mismatch")
        result=_invoke_step(producer,scope,budget,monotonic,cancel_requested)
    journal.finish(round_id,family,_with_plan_gaps(result,producer.plan))
    if observe is not None:
        confirmed=journal.family(round_id,family)
        if confirmed is not None:
            observe(confirmed)
    return True
