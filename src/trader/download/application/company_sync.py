"""One finite full-company round; every family keeps its own truthful outcome."""

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from typing import Protocol

from trader.download.domain.company_sync import (
    CompanySourceCancelled,
    CompanySyncBudget,
    CompanySyncFamily,
    CompanySyncFamilyResult,
    CompanySyncInterrupted,
    CompanySyncPauseReason,
    CompanySyncPhasePlan,
    CompanySyncPhaseState,
    CompanySyncRoundResult,
    CompanySyncRoundScope,
    CompanySyncSourceBudgetExceeded,
    CompanySyncStepResult,
    CompanySyncUnresolvedDirectoryRequest,
)
from trader.download.domain.company_evidence import CompanyInputValidationFailure, CompanySourceFailure
from trader.download.domain.download_failure import DownloadValidationFailure
from trader.download.application.company_failures import CompanyFailureCode, IsRetryableCompanySourceFailure


_RECOMMENDATION_FAMILIES = frozenset({
    CompanySyncFamily.FINANCIAL,
    CompanySyncFamily.PLEDGE,
    CompanySyncFamily.UNLOCK,
    CompanySyncFamily.ANNOUNCEMENTS,
    CompanySyncFamily.FIELDS,
})


@dataclass(frozen=True, slots=True)
class CompanyDirectoryResponseReceipt:
    round_id: str
    step: int
    attempt: int
    request_hash: str
    state_hash: str
    request_body: bytes
    response_content: bytes
    encoding: str


CompanyDirectoryRecovery = Callable[
    [CompanySyncRoundScope, CompanyDirectoryResponseReceipt], CompanySyncStepResult | None,
]


class CompanySyncProducer(Protocol):
    @property
    def plan(self)->CompanySyncPhasePlan: ...
    def state(self,scope:CompanySyncRoundScope)->CompanySyncPhaseState: ...
    def advance(self,scope:CompanySyncRoundScope,*,cancel_requested:Callable[[],bool],deadline:float)->CompanySyncStepResult: ...


class CompanySyncRoundJournal(Protocol):
    def begin(self,scope:CompanySyncRoundScope,plans:tuple[CompanySyncPhasePlan,...],*,max_source_calls:int | None)->str: ...
    def source_calls(self,round_id:str)->int: ...
    def family(self,round_id:str,family:CompanySyncFamily)->CompanySyncFamilyResult|None: ...
    def outstanding(self,round_id:str,family:CompanySyncFamily)->CompanySyncPhaseState|None: ...
    def outstanding_calls(self,round_id:str,family:CompanySyncFamily)->int: ...
    def outstanding_has_unobserved_request(self, round_id: str, family: CompanySyncFamily) -> bool: ...
    def outstanding_family(self,round_id:str)->CompanySyncFamily|None: ...
    def completed_state(self,round_id:str,family:CompanySyncFamily)->CompanySyncPhaseState|None: ...
    def start(self,round_id:str,family:CompanySyncFamily,state:CompanySyncPhaseState)->None: ...
    def finish(self,round_id:str,family:CompanySyncFamily,result:CompanySyncStepResult)->None: ...
    def directory_response(self, round_id: str) -> CompanyDirectoryResponseReceipt | None: ...
    def confirm_directory_recovery(self, receipt: CompanyDirectoryResponseReceipt, result: CompanySyncStepResult) -> None: ...
    def publication_identity(self)->str: ...


@dataclass
class _FamilyProgress:
    producer: CompanySyncProducer
    steps: int = 0
    result: CompanySyncFamilyResult | None = None
    deferred: bool = False
    retry_at: float | None = None


def RunCompanySyncRound(
    journal:CompanySyncRoundJournal,
    producers:tuple[CompanySyncProducer,...],
    scope:CompanySyncRoundScope,
    budget:CompanySyncBudget,
    *,
    monotonic:Callable[[],float],
    cancel_requested:Callable[[],bool],
    observe:Callable[[CompanySyncFamilyResult],None]|None=None,
    activity: Callable[[CompanySyncFamily], AbstractContextManager[None]] | None = None,
    recover_directory: CompanyDirectoryRecovery | None = None,
    source_ready: Callable[[], bool] | None = None,
    after_resume: Callable[[CompanySyncRoundScope], None] | None = None,
    sleeper: Callable[[float], None] | None = None,
    transient_retry_seconds: float | None = None,
)->CompanySyncRoundResult:
    """Run bounded steps, retain accepted work and never reissue an unconfirmed request.

    Rotate through families that supply recommendation evidence first. Optional
    inputs run when that group has no step left in this invocation. Recover the
    one outstanding intent ahead of both groups.
    """
    if tuple(producer.plan.family for producer in producers)!=scope.families:
        raise ValueError("company_sync_producers_invalid")
    round_id=journal.begin(scope,tuple(producer.plan for producer in producers),max_source_calls=budget.max_source_calls)
    recovered_directory = (
        not cancel_requested() and monotonic() < budget.deadline
        and _restore_directory(journal, producers, scope, round_id, recover_directory, activity, observe)
    )
    steps = int(recovered_directory)
    stopped=False
    pause_reason: CompanySyncPauseReason | None = None
    pending_family = journal.outstanding_family(round_id)
    if after_resume is not None and pending_family is None and not cancel_requested() and monotonic() < budget.deadline:
        after_resume(scope)
    ordered_producers = sorted(producers, key=lambda producer: producer.plan.family not in _RECOMMENDATION_FAMILIES)
    progress = tuple(_FamilyProgress(producer) for producer in ordered_producers)
    for item in progress:
        family = item.producer.plan.family
        if recovered_directory and family is CompanySyncFamily.ANNOUNCEMENTS:
            item.steps = 1
            item.result = journal.family(round_id, family)
        elif family not in _RECOMMENDATION_FAMILIES and not cancel_requested() and monotonic() < budget.deadline:
            item.result = journal.family(round_id, family)
    pass_progress = tuple(sorted(progress, key=lambda item: item.producer.plan.family != pending_family))
    while not stopped:
        advanced=False
        recommendation_advanced = False
        for item in pass_progress:
            producer=item.producer
            family=producer.plan.family
            recommendation = family in _RECOMMENDATION_FAMILIES
            if not recommendation and recommendation_advanced:
                continue
            if item.deferred or (budget.steps_per_family is not None and item.steps >= budget.steps_per_family):
                continue
            cancelled = cancel_requested()
            if cancelled or monotonic()>=budget.deadline:
                pause_reason = None if cancelled else "deadline"
                stopped=True
                break
            if item.retry_at is not None:
                if monotonic() < item.retry_at:
                    continue
                item.retry_at = None
            current=journal.family(round_id,family)
            if current is not None:
                item.result=current
            state = None
            refresh = False
            if current is not None and producer.plan.refresh_on_input_change:
                completed = journal.completed_state(round_id, family)
                state = producer.state(scope)
                refresh = completed != state
            if current is not None and not current.result.can_continue and not refresh and family is not pending_family:
                if "source_budget_exhausted" not in current.result.gaps:
                    continue
                if budget.max_source_calls is not None:
                    pause_reason = "request_limit"
                    continue
            if budget.max_steps is not None and steps >= budget.max_steps:
                pause_reason = "step_limit"
                stopped=True
                break
            if state is None:
                state = producer.state(scope)
            outstanding=journal.outstanding(round_id,family)
            reconciled=outstanding is not None and (outstanding.source_required or state.source_required
                or outstanding.identity!=state.identity)
            if reconciled and outstanding is not None:
                with activity(family) if activity is not None else nullcontext():
                    result=_unconfirmed_source_result(journal,round_id,family,outstanding,state) or CompanySyncStepResult(
                        False,True,accepted_inputs=None,gaps=("accepted_input_count_unconfirmed",))
            else:
                source_budget_exhausted = (state.source_required and budget.max_source_calls is not None
                    and journal.source_calls(round_id) >= budget.max_source_calls)
                if outstanding is None and state.source_required and not source_budget_exhausted and source_ready is not None and not source_ready():
                    if pause_reason is None:
                        pause_reason = "source_window"
                    item.deferred = True
                    continue
                if outstanding is None:
                    journal.start(round_id,family,state)
                if source_budget_exhausted:
                    result=CompanySyncStepResult(False,False,gaps=("source_budget_exhausted",))
                else:
                    try:
                        with activity(family) if activity is not None else nullcontext():
                            result=_invoke_step(producer,scope,budget,monotonic,cancel_requested,
                                transient_retry_seconds=transient_retry_seconds)
                            if IsRetryableCompanySourceFailure(result.failure) and journal.outstanding_has_unobserved_request(round_id,family):
                                result = CompanySyncStepResult(False, False, gaps=("source_attempt_unconfirmed",), failure="source_attempt_unconfirmed")
                    except CompanySyncInterrupted:
                        item.result=_interrupted_family(item.result,producer.plan,state.source_required)
                        pause_reason = None if cancel_requested() else "deadline"
                        stopped=True
                        break
                if "source_budget_exhausted" in result.gaps:
                    pause_reason = "request_limit"
            if (result.deferred and result.retry_after_seconds is not None and state.source_required
                and budget.max_source_calls is not None and journal.source_calls(round_id) >= budget.max_source_calls):
                result = replace(result, retry_after_seconds=None)
                pause_reason = "request_limit"
            steps+=1
            item.steps+=1
            advanced=True
            recommendation_advanced = recommendation_advanced or recommendation
            journal.finish(round_id,family,_with_plan_gaps(result,producer.plan))
            finished_resume = family is pending_family
            if finished_resume:
                pending_family = None
            current=journal.family(round_id,family)
            if current is not None:
                item.result=current
                if observe is not None:
                    observe(current if result.retry_after_seconds is None else replace(current,
                        result=replace(current.result, retry_after_seconds=result.retry_after_seconds)))
            if finished_resume and after_resume is not None and not cancel_requested() and monotonic() < budget.deadline:
                after_resume(scope)
            if result.deferred and not reconciled:
                if sleeper is not None and result.retry_after_seconds is not None:
                    item.retry_at = monotonic() + result.retry_after_seconds
                else:
                    item.deferred=True
            elif not result.can_continue and "company_raw_capacity_unavailable" in result.gaps:
                # Retry admission on the next invocation. Other families can
                # finish their local work without looping on the same disk.
                item.deferred = True
            if cancel_requested():
                stopped=True
                break
        if not advanced:
            waits = [item.retry_at for item in progress if item.retry_at is not None
                and (budget.steps_per_family is None or item.steps < budget.steps_per_family)]
            if stopped or not waits or sleeper is None:
                break
            if budget.max_steps is not None and steps >= budget.max_steps:
                pause_reason = "step_limit"
                break
            wake_at = min(min(waits), budget.deadline)
            while (remaining := wake_at - monotonic()) > 0 and not cancel_requested():
                sleeper(min(remaining, 0.5))
        pass_progress = progress
    # Do not open another database after cancellation to manufacture a final
    # inventory or publication count. The last acknowledged head stays explicit.
    publication="" if cancel_requested() or monotonic()>=budget.deadline else journal.publication_identity()
    ordered_results=tuple(sorted((item.result for item in progress if item.result is not None),
        key=lambda result:tuple(CompanySyncFamily).index(result.family)))
    cancelled = cancel_requested()
    return CompanySyncRoundResult(
        round_id=round_id,
        scope=scope,
        families=ordered_results,
        cancelled=cancelled,
        budget_exhausted=pause_reason is not None,
        publication_hash=publication,
        pause_reason=None if cancelled else pause_reason,
    )


def _with_plan_gaps(result:CompanySyncStepResult,plan:CompanySyncPhasePlan)->CompanySyncStepResult:
    gaps=plan.qualification_gaps+tuple(gap for gap in result.gaps if gap not in plan.qualification_gaps)
    return replace(result,gaps=gaps)


def _restore_directory(
    journal: CompanySyncRoundJournal,
    producers: tuple[CompanySyncProducer, ...],
    scope: CompanySyncRoundScope,
    round_id: str,
    recover: CompanyDirectoryRecovery | None,
    activity: Callable[[CompanySyncFamily], AbstractContextManager[None]] | None,
    observe: Callable[[CompanySyncFamilyResult], None] | None,
) -> bool:
    if recover is None:
        return False
    receipt = journal.directory_response(round_id)
    if receipt is None:
        return False
    family = CompanySyncFamily.ANNOUNCEMENTS
    producer = next(item for item in producers if item.plan.family is family)
    with activity(family) if activity is not None else nullcontext():
        result = recover(scope, receipt)
        if result is None:
            return False
        journal.confirm_directory_recovery(receipt, _with_plan_gaps(result, producer.plan))
    if observe is not None:
        confirmed = journal.family(round_id, family)
        if confirmed is not None:
            observe(confirmed)
    return True


def _unconfirmed_source_result(
    journal: CompanySyncRoundJournal, round_id: str, family: CompanySyncFamily,
    outstanding: CompanySyncPhaseState, current: CompanySyncPhaseState,
) -> CompanySyncStepResult | None:
    if not outstanding.source_required:
        if current.source_required and outstanding.identity == current.identity:
            return CompanySyncStepResult(False, True, deferred=True)
        return None
    if outstanding.identity != current.identity:
        # A different selection can come from queue ordering alone.
        if not journal.outstanding_has_unobserved_request(round_id, family):
            return None
    elif current.local_input_verified:
        return None
    elif journal.outstanding_calls(round_id, family) == 0:
        return CompanySyncStepResult(False, True, deferred=True)
    return CompanySyncStepResult(False, False, gaps=("source_attempt_unconfirmed",), failure="source_attempt_unconfirmed")


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
    monotonic:Callable[[],float],cancel_requested:Callable[[],bool], *,
    transient_retry_seconds: float | None = None)->CompanySyncStepResult:
    try:
        return producer.advance(scope,cancel_requested=cancel_requested,deadline=budget.deadline)
    except CompanySyncSourceBudgetExceeded:
        return CompanySyncStepResult(False,False,gaps=("source_budget_exhausted",))
    except CompanySyncUnresolvedDirectoryRequest:
        code = "previous_directory_attempt_unconfirmed"
        return CompanySyncStepResult(False, False, gaps=(code,), failure=code)
    except InterruptedError:
        raise
    except CompanySourceCancelled as exc:
        if not cancel_requested() and monotonic()<budget.deadline:
            raise
        raise CompanySyncInterrupted("company_sync_source_interrupted") from exc
    except CompanySourceFailure as exc:
        code = CompanyFailureCode(exc)
        if IsRetryableCompanySourceFailure(code):
            return CompanySyncStepResult(False, True, deferred=True, failure=code, gaps=("source_retry_pending",),
                retry_after_seconds=transient_retry_seconds)
        return CompanySyncStepResult(False,False,failure=code,gaps=("supplier_input_rejected",))
    except (CompanyInputValidationFailure,DownloadValidationFailure) as exc:
        return CompanySyncStepResult(False,False,failure=CompanyFailureCode(exc),gaps=("supplier_input_rejected",))


def ResumeCompanySyncPendingStep(journal:CompanySyncRoundJournal,producers:tuple[CompanySyncProducer,...],
    scope:CompanySyncRoundScope,round_id:str,budget:CompanySyncBudget,*,monotonic:Callable[[],float],cancel_requested:Callable[[],bool],
    observe:Callable[[CompanySyncFamilyResult],None]|None=None,
    activity: Callable[[CompanySyncFamily], AbstractContextManager[None]] | None = None,
    recover_directory: CompanyDirectoryRecovery | None = None,
)->bool:
    """Reconcile one old intent before a new local prerequisite; never start HTTP."""
    family=journal.outstanding_family(round_id)
    if family is None and recover_directory is None:
        return False
    if cancel_requested() or monotonic()>=budget.deadline:
        raise CompanySyncInterrupted("company_sync_recovery_interrupted")
    if _restore_directory(journal, producers, scope, round_id, recover_directory, activity, observe):
        return True
    if family is None:
        return False
    producer=next(item for item in producers if item.plan.family is family)
    old=journal.outstanding(round_id,family)
    if old is None:
        raise ValueError("company_sync_pending_owner_changed")
    with activity(family) if activity is not None else nullcontext():
        current=producer.state(scope)
        result=_unconfirmed_source_result(journal,round_id,family,old,current)
        if result is None:
            if old.identity!=current.identity:
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
