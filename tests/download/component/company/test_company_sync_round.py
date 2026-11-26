"""Persisted scope and transport slots protect retries across process restarts."""

from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.application.company_round_progress import CompanyRequestProgress
from trader.download.application.company_sync import ResumeCompanySyncPendingStep, RunCompanySyncRound
from trader.download.application.sync_data import SyncResult, SyncTaskResult
from trader.download.domain.company_sync import (
    CompanySyncBudget, CompanySyncFamily, CompanySyncPhasePlan, CompanySyncPhaseState, CompanySyncRoundScope, CompanySyncStepResult, CompanySyncInterrupted,
    CompanySyncSourceBudgetExceeded, CompanySyncUnresolvedDirectoryRequest,
)
from trader.download.entrypoints.company_result_feedback import FormatCompanySyncSummary
from trader.download.infra.company_disclosure_source import CNINFO_DIRECTORY_URL
from trader.download.infra.company_sync_inputs import SQLiteCompanySyncInputStore
from trader.download.infra.company_sync_io import CompanySyncIoBudget
from trader.download.infra.company_sync_journal import SQLiteCompanySyncRoundJournal
from trader.infra.http_contracts import HttpAttempt, HttpRequest, HttpResponse
from trader.download.domain.company_sync import CompanySourceCancelled
from trader.infra.http_transport import RequestsHttpTransport
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession


_SCOPE = CompanySyncRoundScope(("600001",), datetime(2026, 11, 17, tzinfo=ZoneInfo("Asia/Shanghai")))


class _Producer:
    def __init__(self, family, inputs, transport, calls, *, crash=False, cancel=None):
        self.plan = CompanySyncPhasePlan(family, CanonicalDigest(family))
        self.inputs, self.transport, self.calls = inputs, transport, calls
        self.crash, self.cancel = crash, cancel

    def _cursor(self, scope):
        return next((page for page in (1, 2) if not self.inputs.task_done(scope.identity, self.plan.family.value, str(page))), None)

    def state(self, scope):
        page = self._cursor(scope) if self.plan.family is CompanySyncFamily.FINANCIAL else None
        return CompanySyncPhaseState(CanonicalDigest((self.plan.family, page)), page is not None)

    def advance(self, scope, *, cancel_requested, deadline):
        page = self._cursor(scope) if self.plan.family is CompanySyncFamily.FINANCIAL else None
        if page is None:
            self.calls.append(self.plan.family.value)
            return CompanySyncStepResult(True, False, pending_count=0)
        self.transport.get(f"https://example.test/{page}", timeout=1)
        if self.cancel is not None:
            self.cancel[0] = True
            raise CompanySyncInterrupted("after_response_before_accept")
        self.inputs.complete_task(scope.identity, self.plan.family.value, str(page), 1, CanonicalDigest(page))
        if self.crash:
            self.crash = False
            raise RuntimeError("after_accept_before_ack")
        return CompanySyncStepResult(False, True, accepted_inputs=1, source_calls=1)


def _resources(tmp_path, *, maximum=8, crash=False, cancel=None):
    policy = SqlitePolicyFixture()
    journal = SQLiteCompanySyncRoundJournal(tmp_path / "journal.sqlite3", sqlite_policy=policy, publication_identity=lambda: "a" * 64)
    journal.initialize()
    inputs = SQLiteCompanySyncInputStore(tmp_path / "inputs.sqlite3", sqlite_policy=policy)
    inputs.initialize()
    physical, calls = [], []
    def execute(request):
        physical.append(request.url)
        return HttpResponse(200, b"ok")
    transport = RequestsHttpTransport(
        authorize=journal.authorize_request, observe=journal.observe_attempt, execute_request=execute, monotonic=lambda: 0,
    )
    producers = tuple(_Producer(family, inputs, transport, calls, crash=crash and family is CompanySyncFamily.FINANCIAL,
        cancel=cancel if family is CompanySyncFamily.FINANCIAL else None) for family in _SCOPE.families)
    budget = CompanySyncBudget(100, maximum, 10, 10)
    return journal, producers, budget, physical, calls, transport


def test_real_journal_counts_multiple_pages_and_restart_reuses_fixed_scope_without_http(tmp_path):
    journal, producers, budget, physical, calls, transport = _resources(tmp_path)
    with transport:
        result = RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: False)
    assert result.business_complete and result.scope == _SCOPE
    assert len(physical) == result.families[0].source_calls == 2
    assert result.families[0].unknown_attempts == 0
    restarted, producers, budget, physical, calls, transport = _resources(tmp_path)
    assert restarted.active().scope == _SCOPE
    with transport:
        again = RunCompanySyncRound(restarted, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: False)
    assert again == result and physical == [] and calls == []
    with pytest.raises(ValueError, match="active_scope_must_resume"):
        restarted.begin(CompanySyncRoundScope(("600002",), _SCOPE.cutoff), tuple(item.plan for item in producers), max_source_calls=8)


def test_accept_before_ack_crash_reconciles_old_step_before_authorizing_next_page(tmp_path):
    journal, producers, budget, physical, calls, transport = _resources(tmp_path, crash=True)
    with transport, pytest.raises(RuntimeError, match="after_accept_before_ack"):
        RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: False)
    assert len(physical) == 1
    journal, producers, budget, physical, calls, transport = _resources(tmp_path)
    with transport:
        result = RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: False)
    assert result.processing_complete and not result.business_complete and physical == ["https://example.test/2"]
    assert result.families[0].accepted_inputs is None
    assert result.families[0].source_calls == 2


def test_closed_financial_publication_runs_after_acknowledging_pending_fields(tmp_path):
    journal, producers, budget, physical, calls, transport = _resources(tmp_path)
    round_id = journal.begin(_SCOPE, tuple(producer.plan for producer in producers), max_source_calls=budget.max_source_calls)
    fields = next(producer for producer in producers if producer.plan.family is CompanySyncFamily.FIELDS)
    journal.start(round_id, fields.plan.family, fields.state(_SCOPE))

    def publish(scope):
        assert scope == _SCOPE and journal.outstanding_family(round_id) is None
        assert journal.family(round_id, fields.plan.family).result.processed_scope_complete
        assert calls == ["fields"]
        calls.append("financial_scoring")

    with transport:
        RunCompanySyncRound(
            journal, producers, _SCOPE, replace(budget, max_steps=1),
            monotonic=lambda: 0, cancel_requested=lambda: False, after_resume=publish,
        )
    assert calls == ["fields", "financial_scoring"] and physical == []


def test_progress_observes_only_durable_step_results_without_extra_requests(tmp_path):
    journal, producers, budget, physical, _, transport = _resources(tmp_path)
    observations = []

    def observe(family):
        assert journal.outstanding(journal.active().round_id, family.family) is None
        assert journal.family(journal.active().round_id, family.family) == family
        observations.append(family)

    with transport:
        result = RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0,
            cancel_requested=lambda: False, observe=observe)
    latest = {item.family: item for item in observations}
    assert tuple(latest[family.family] for family in result.families) == result.families
    assert len(physical) == 2 and result.business_complete


def test_progress_failure_keeps_acknowledged_page_and_restarts_without_refetch(tmp_path):
    journal, producers, budget, physical, _, transport = _resources(tmp_path)

    def fail(family):
        raise RuntimeError("fixture_progress_unavailable")

    with transport, pytest.raises(RuntimeError, match="fixture_progress_unavailable"):
        RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0,
            cancel_requested=lambda: False, observe=fail)
    assert len(physical) == 1 and journal.outstanding_family(journal.active().round_id) is None
    journal, producers, budget, physical, _, transport = _resources(tmp_path)
    with transport:
        result = RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0,
            cancel_requested=lambda: False)
    assert physical == ["https://example.test/2"] and result.business_complete


def test_unknown_response_acceptance_never_repeats_source_but_later_local_families_finish(tmp_path):
    cancelled = [False]
    journal, producers, budget, physical, calls, transport = _resources(tmp_path, cancel=cancelled)
    with transport:
        result = RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: cancelled[0])
    assert result.cancelled and len(physical) == 1
    assert result.pause_reason is None
    interrupted=result.families[0]
    assert interrupted.source_calls is None and interrupted.unknown_attempts is None
    assert interrupted.accepted_inputs is None and "request_counts_unconfirmed" in interrupted.result.gaps
    journal, producers, budget, physical, calls, transport = _resources(tmp_path)
    with transport:
        result = RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: False)
    assert result.processing_complete and not result.business_complete and not physical
    assert result.families[0].result.failure == "source_attempt_unconfirmed"
    assert "fields" in calls
    with pytest.raises(ValueError, match="requires_terminal_scope"):
        journal.finish_refresh(result.round_id)


def test_unlimited_resume_finishes_old_budget_paused_page_without_reissuing_requests(tmp_path):
    journal, producers, budget, physical, _, transport = _resources(tmp_path, maximum=1)
    with transport:
        limited = RunCompanySyncRound(journal, producers, _SCOPE, budget,
            monotonic=lambda: 0, cancel_requested=lambda: False)
    assert limited.pause_reason == "request_limit" and physical == ["https://example.test/1"]
    journal, producers, _, physical, _, transport = _resources(tmp_path)
    unlimited = CompanySyncBudget(None, None, None, float("inf"))
    with transport:
        completed = RunCompanySyncRound(journal, producers, _SCOPE, unlimited,
            monotonic=lambda: 86400, cancel_requested=lambda: False)
    stored = journal.active()
    assert stored is not None and stored.max_source_calls is None and stored.source_calls == 2
    assert completed.round_id == limited.round_id and completed.business_complete
    assert physical == ["https://example.test/2"] and completed.pause_reason is None


def test_fixed_source_budget_exhaustion_still_runs_local_fields_and_does_not_refill_on_resume(tmp_path):
    journal, producers, budget, physical, calls, transport = _resources(tmp_path, maximum=1)
    with transport:
        result = RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: False)
    assert result.budget_exhausted and result.processing_complete and not result.business_complete
    assert result.pause_reason == "request_limit"
    assert FormatCompanySyncSummary(SyncResult((SyncTaskResult("company", "pending", company=result),)), "暂停").splitlines() == [
        "公司同步：暂停（固定轮次的来源请求额度已用完），范围 1 股，断点已保留",
        "来源库：财务请求额度用尽、质押采集处理结束、解禁采集处理结束、公告采集处理结束、年度营收采集处理结束。",
        "正式库：已确认发布，业务资料仍有缺口。",
        "待处理：财务。",
        "请求账本：已占用 1 次，未确认 0 次。",
    ]
    assert len(physical) == 1 and "fields" in calls
    journal, producers, budget, physical, calls, transport = _resources(tmp_path, maximum=1)
    with transport:
        result = RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: False)
    assert not physical and journal.source_calls(result.round_id) == 1
    assert result.pause_reason == "request_limit"


def test_source_reserve_does_not_preallocate_next_page_and_keeps_local_work(tmp_path):
    journal, producers, budget, physical, calls, transport = _resources(tmp_path)
    clock = [0.0]
    advance = producers[0].advance

    def advance_first_page(scope, *, cancel_requested, deadline):
        result = advance(scope, cancel_requested=cancel_requested, deadline=deadline)
        clock[0] = 9.0
        return result

    producers[0].advance = advance_first_page
    with transport:
        first = RunCompanySyncRound(
            journal, producers, _SCOPE, budget, monotonic=lambda: clock[0],
            cancel_requested=lambda: False, source_ready=lambda: clock[0] < 8,
        )
    assert first.paused and not first.processing_complete
    assert first.pause_reason == "source_window"
    assert physical == ["https://example.test/1"] and "fields" in calls
    assert journal.source_calls(first.round_id) == 1
    assert journal.outstanding_family(first.round_id) is None

    restarted, producers, budget, physical, calls, transport = _resources(tmp_path)
    with transport:
        second = RunCompanySyncRound(
            restarted, producers, _SCOPE, budget, monotonic=lambda: 0,
            cancel_requested=lambda: False, source_ready=lambda: True,
        )
    assert second.business_complete and second.round_id == first.round_id
    assert physical == ["https://example.test/2"] and calls == ["financial"]
    assert restarted.source_calls(second.round_id) == 2


def test_recommendation_families_rotate_before_optional_inputs(tmp_path):
    journal, producers, budget, physical, calls, transport = _resources(tmp_path)
    order = []
    with transport:
        result = RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0,
            cancel_requested=lambda: False, observe=lambda family: order.append(family.family))
    assert result.business_complete
    assert order == [
        CompanySyncFamily.FINANCIAL, CompanySyncFamily.PLEDGE, CompanySyncFamily.UNLOCK,
        CompanySyncFamily.ANNOUNCEMENTS, CompanySyncFamily.FIELDS, CompanySyncFamily.FINANCIAL,
        CompanySyncFamily.FINANCIAL, CompanySyncFamily.ANNUAL_INCOME,
    ]
    assert tuple(item.family for item in result.families) == _SCOPE.families
    assert len(physical) == 2


def test_small_step_allowance_reaches_recommendation_fields_before_optional_inputs(tmp_path):
    journal, producers, budget, physical, calls, transport = _resources(tmp_path)
    budget = CompanySyncBudget(5, 8, 10, 10)
    with transport:
        result = RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: False)
    assert not result.processing_complete and result.budget_exhausted
    assert result.pause_reason == "step_limit"
    assert len(physical) == 1 and calls == ["pledge", "unlock", "announcements", "fields"]
    assert result.families[-1].family is CompanySyncFamily.FIELDS
    assert result.families[-1].result.processed_scope_complete


def test_optional_inputs_run_after_core_family_reaches_its_invocation_limit(tmp_path):
    journal, producers, budget, physical, calls, transport = _resources(tmp_path)
    budget = CompanySyncBudget(100, 8, 1, 10)
    with transport:
        result = RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: False)
    assert not result.processing_complete and not result.budget_exhausted
    assert result.pause_reason is None
    assert len(physical) == 1
    assert calls == ["pledge", "unlock", "announcements", "fields", "annual_income"]

    restarted, producers, budget, physical, calls, transport = _resources(tmp_path)
    short = CompanySyncBudget(1, budget.max_source_calls, budget.steps_per_family, budget.deadline)
    with transport:
        resumed = RunCompanySyncRound(restarted, producers, _SCOPE, short, monotonic=lambda: 0, cancel_requested=lambda: False)
    assert resumed.round_id == result.round_id
    assert resumed.families[4:6] == result.families[4:6]
    assert physical == ["https://example.test/2"] and not calls


@pytest.mark.parametrize("preflight", (False, True))
def test_unobserved_pending_request_stays_blocked_if_priority_changes_selected_input(tmp_path, preflight):
    journal, producers, budget, physical, calls, transport = _resources(tmp_path)
    plans = tuple(producer.plan for producer in producers)
    round_id = journal.begin(_SCOPE, plans, max_source_calls=budget.max_source_calls)
    pending_family = CompanySyncFamily.ANNOUNCEMENTS
    journal.start(round_id, pending_family, CompanySyncPhaseState(CanonicalDigest("previous_document"), True))
    journal.authorize_request(HttpRequest("GET", "https://example.test/previous-document", 1))

    restarted, producers, budget, physical, calls, transport = _resources(tmp_path)
    observed = []
    with transport:
        if preflight:
            assert ResumeCompanySyncPendingStep(restarted, producers, _SCOPE, round_id, budget,
                monotonic=lambda: 0, cancel_requested=lambda: False, observe=observed.append)
        result = RunCompanySyncRound(restarted, producers, _SCOPE, budget,
            monotonic=lambda: 0, cancel_requested=lambda: False, observe=observed.append)

    assert observed[0].family is pending_family
    assert observed[0].result.failure == "source_attempt_unconfirmed"
    assert observed[0].unknown_attempts == 1
    assert result.round_id == round_id and not result.business_complete
    assert physical == ["https://example.test/1", "https://example.test/2"]
    assert "announcements" not in calls and "fields" in calls
    assert restarted.source_calls(round_id) == 3


def test_finishing_exactly_at_step_limit_does_not_invent_budget_exhaustion(tmp_path):
    journal, producers, budget, physical, calls, transport = _resources(tmp_path)
    budget = CompanySyncBudget(9, 8, 10, 10)
    with transport:
        result = RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: False)
    assert result.business_complete and not result.budget_exhausted and len(physical) == 2


def test_transport_rejection_and_unobserved_slot_cannot_be_reported_as_confirmed_http(tmp_path):
    journal, producers, budget, physical, calls, transport = _resources(tmp_path, maximum=1)
    plans = tuple(item.plan for item in producers)
    round_id = journal.begin(_SCOPE, plans, max_source_calls=1)
    journal.start(round_id, CompanySyncFamily.FINANCIAL, producers[0].state(_SCOPE))
    journal.authorize_request(HttpRequest("GET", "https://example.test/1", 1))
    journal.finish(round_id, CompanySyncFamily.FINANCIAL, CompanySyncStepResult(False, False, failure="source_attempt_unconfirmed"))
    assert journal.family(round_id, CompanySyncFamily.FINANCIAL).unknown_attempts == 1
    assert physical == []
    transport.close()


def _blocked_directory_round(journal, producers, budget, request, directory_state, *, retain_response=False):
    plans = tuple(producer.plan for producer in producers)
    round_id = journal.begin(_SCOPE, plans, max_source_calls=budget.max_source_calls)
    for family in _SCOPE.families:
        if family is CompanySyncFamily.ANNOUNCEMENTS:
            journal.start(round_id, family, directory_state)
            journal.authorize_request(request)
            if retain_response:
                journal.retain_directory_response(request, HttpResponse(200, b'{"announcements":[]}'))
            journal.finish(round_id, family, CompanySyncStepResult(False, False, failure="source_attempt_unconfirmed"))
        else:
            journal.start(round_id, family, CompanySyncPhaseState(CanonicalDigest(family), False))
            journal.finish(round_id, family, CompanySyncStepResult(True, False, pending_count=0))
    return round_id, plans


def test_later_round_preserves_old_unknown_and_blocks_exact_request_or_cursor(tmp_path):
    journal, producers, budget, _, _, transport = _resources(tmp_path)
    request = HttpRequest("POST", CNINFO_DIRECTORY_URL, 1, body=b"page=1")
    directory = CompanySyncPhaseState(CanonicalDigest("old_directory"), True)
    old, plans = _blocked_directory_round(journal, producers, budget, request, directory)
    assert not journal.defer_unconfirmed_directory(old, _SCOPE.cutoff + timedelta(hours=1))
    assert not journal.can_refresh(old)

    following = replace(_SCOPE, cutoff=_SCOPE.cutoff + timedelta(days=1))
    assert journal.defer_unconfirmed_directory(old, following.cutoff)
    assert journal.active() is None
    blocked = journal.family(old, CompanySyncFamily.ANNOUNCEMENTS)
    assert blocked is not None and blocked.unknown_attempts == journal.source_calls(old) == 1

    new = journal.begin(following, plans, max_source_calls=budget.max_source_calls)
    journal.start(new, CompanySyncFamily.ANNOUNCEMENTS, directory)
    with pytest.raises(CompanySyncUnresolvedDirectoryRequest):
        journal.authorize_request(replace(request, body=b"page=1&new_request_parameter=1"))
    journal.finish(new, CompanySyncFamily.ANNOUNCEMENTS, CompanySyncStepResult(False, False))
    journal.start(new, CompanySyncFamily.ANNOUNCEMENTS, replace(directory, identity=CanonicalDigest("new_directory")))
    with pytest.raises(CompanySyncUnresolvedDirectoryRequest):
        journal.authorize_request(request)
    assert journal.source_calls(new) == 0
    journal.authorize_request(replace(request, body=b"page=2&new_scope=1"))
    assert journal.source_calls(new) == journal.source_calls(old) == 1
    transport.close()


def test_later_round_preserves_recoverable_old_directory_round(tmp_path):
    journal, producers, budget, _, _, transport = _resources(tmp_path)
    request = HttpRequest("POST", CNINFO_DIRECTORY_URL, 1, body=b"page=1")
    directory = CompanySyncPhaseState(CanonicalDigest("old_directory"), True)
    old, _ = _blocked_directory_round(journal, producers, budget, request, directory, retain_response=True)
    assert journal.directory_response(old) is not None
    assert not journal.defer_unconfirmed_directory(old, _SCOPE.cutoff + timedelta(days=1))
    assert journal.active().round_id == old
    transport.close()


def test_cancel_before_authorization_is_safe_to_resume_without_losing_allowance(tmp_path):
    cancelled = [False]
    journal, producers, budget, physical, calls, transport = _resources(tmp_path)
    def interrupted(scope, *, cancel_requested, deadline):
        cancelled[0] = True
        raise CompanySourceCancelled("source_pacing_cancelled")
    producers[0].advance = interrupted
    with transport:
        result = RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: cancelled[0])
    assert result.cancelled and not physical and journal.source_calls(result.round_id) == 0
    journal, producers, budget, physical, calls, transport = _resources(tmp_path)
    with transport:
        result = RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: False)
    assert result.business_complete and len(physical) == 2 and result.families[0].unknown_attempts == 0


def test_later_local_outstanding_is_resolved_before_earlier_family_requests_resume(tmp_path):
    journal, producers, budget, physical, calls, transport = _resources(tmp_path)
    def interrupted(scope, *, cancel_requested, deadline):
        raise RuntimeError("fields_before_ack")
    producers[-1].advance = interrupted
    short = CompanySyncBudget(100, 8, 1, 10)
    with transport, pytest.raises(RuntimeError, match="fields_before_ack"):
        RunCompanySyncRound(journal, producers, _SCOPE, short, monotonic=lambda: 0, cancel_requested=lambda: False)
    assert physical == ["https://example.test/1"]
    journal, producers, budget, physical, calls, transport = _resources(tmp_path)
    with transport:
        result = RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: False)
    assert result.business_complete and physical == ["https://example.test/2"]


def test_reopened_field_intent_is_reconciled_before_source_when_watermark_returns_to_completed_value(tmp_path):
    journal, producers, budget, physical, calls, transport = _resources(tmp_path)
    fields = producers[-1]
    fields.plan = replace(fields.plan, refresh_on_input_change=True)
    round_id = journal.begin(_SCOPE, tuple(item.plan for item in producers), max_source_calls=budget.max_source_calls)
    journal.start(round_id, CompanySyncFamily.FIELDS, fields.state(_SCOPE))
    journal.finish(round_id, CompanySyncFamily.FIELDS, CompanySyncStepResult(True, False, pending_count=0))
    journal.start(round_id, CompanySyncFamily.FIELDS, CompanySyncPhaseState(CanonicalDigest("changed_material"), False))

    events = []
    with transport:
        result = RunCompanySyncRound(journal, producers, _SCOPE, budget,
            monotonic=lambda: 0, cancel_requested=lambda: False, observe=events.append)
    assert events[0].family is CompanySyncFamily.FIELDS
    assert events[0].result.gaps == ("accepted_input_count_unconfirmed",)
    assert result.business_complete and physical == ["https://example.test/1", "https://example.test/2"]
    assert journal.outstanding_family(round_id) is None


def test_unknown_interrupted_error_is_not_hidden_even_when_deadline_expires(tmp_path):
    journal, producers, budget, physical, calls, transport = _resources(tmp_path)
    clock = [0]
    def interrupted(scope, *, cancel_requested, deadline):
        clock[0] = 11
        raise InterruptedError("unrelated_local_failure")
    producers[0].advance = interrupted
    with transport, pytest.raises(InterruptedError, match="unrelated_local_failure"):
        RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: clock[0], cancel_requested=lambda: False)
    assert not physical


def test_source_deadline_marks_counters_unknown_without_post_deadline_sql(tmp_path):
    from trader.download.infra.company_sync_io import CompanySyncIoBudget
    journal, producers, budget, physical, _, transport = _resources(tmp_path)
    clock = [0.0]
    journal._io_budget = CompanySyncIoBudget(deadline=10, monotonic=lambda: clock[0], cancel_requested=lambda: False)
    original = producers[0].advance

    def expires(scope, *, cancel_requested, deadline):
        original(scope, cancel_requested=cancel_requested, deadline=deadline)
        clock[0] = 11
        raise CompanySyncInterrupted("fixture_deadline_after_request")

    producers[0].advance = expires
    with transport:
        result = RunCompanySyncRound(journal, producers, _SCOPE, budget,
            monotonic=lambda: clock[0], cancel_requested=lambda: False)
    assert result.budget_exhausted and not result.cancelled and len(physical) == 1
    assert result.pause_reason == "deadline"
    family, = result.families
    assert family.source_calls is None and family.unknown_attempts is None
    assert family.accepted_inputs is None and not family.result.business_complete
    with pytest.raises(CompanySyncInterrupted):
        journal.source_calls(result.round_id)


def test_source_interruption_preserves_a_previous_confirmed_failure(tmp_path):
    journal, producers, budget, _, _, transport = _resources(tmp_path)
    annual = next(item for item in producers if item.plan.family is CompanySyncFamily.ANNUAL_INCOME)
    clock, attempts = [0.0], [0]
    annual.state = lambda scope: CompanySyncPhaseState(CanonicalDigest(attempts[0]), True)

    def advance(scope, *, cancel_requested, deadline):
        attempts[0] += 1
        if attempts[0] == 1:
            return CompanySyncStepResult(False, True, failure="annual_fixture_rejected",
                gaps=("annual_income_source_failed",))
        transport.get("https://example.test/annual-next", timeout=1)
        clock[0] = 11
        raise CompanySyncInterrupted("fixture_later_period_deadline")

    annual.advance = advance
    with transport:
        result = RunCompanySyncRound(journal, producers, _SCOPE, budget,
            monotonic=lambda: clock[0], cancel_requested=lambda: False)
    family = next(item for item in result.families if item.family is CompanySyncFamily.ANNUAL_INCOME)
    assert family.result.failure == "annual_fixture_rejected"
    assert "annual_income_source_failed" in family.result.gaps
    assert family.source_calls is None and family.unknown_attempts is None


@pytest.mark.parametrize("failure", ("supplier_validation", "supplier_access", "source_scope"))
def test_known_source_rejection_does_not_skip_later_local_fields(tmp_path, failure):
    from trader.download.domain.company_evidence import CompanyInputValidationFailure, CompanySourceFailure
    from trader.download.domain.download_failure import DownloadValidationFailure
    journal, producers, budget, physical, calls, transport = _resources(tmp_path)
    def rejected(scope, *, cancel_requested, deadline):
        if failure == "supplier_validation":
            raise DownloadValidationFailure("company_source_invalid", "NOTICE_DATE", "date", "missing")
        if failure == "supplier_access":
            raise CompanySourceFailure("access_denied")
        raise CompanyInputValidationFailure("company_annual_income_response_scope_mismatch")
    producers[0].advance = rejected
    with transport:
        result = RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: False)
    assert result.processing_complete and not result.business_complete and "fields" in calls
    assert result.families[0].result.failure and not physical


@pytest.mark.parametrize("outcome", ("recovered", "repeated", "cancelled", "unobserved"))
def test_transport_retry_yields_to_local_work_and_only_retries_acknowledged_requests(tmp_path, outcome):
    from trader.download.domain.company_evidence import CompanySourceFailure
    from trader.download.infra.company_http import CompanySourceOperation
    from trader.infra.http_contracts import HttpConnectionError

    journal, producers, _, physical, calls, transport = _resources(tmp_path)
    clock = [0.0]
    def send(request):
        physical.append(request.url)
        if len(physical) == 1 or outcome == "repeated" and len(physical) <= 5 or outcome == "cancelled":
            raise HttpConnectionError("http_transport_failed")
        return HttpResponse(200, b"ok")
    transport._execute_request = send
    financial = producers[0]
    advance = financial.advance
    @CompanySourceOperation
    def fetch(scope, **kwargs):
        if outcome == "unobserved":
            journal.authorize_request(HttpRequest("GET", "https://example.test/pending", 1))
            raise CompanySourceFailure("timeout")
        return advance(scope, **kwargs)
    financial.advance = fetch

    def sleep(seconds):
        assert "fields" in calls and 0 < seconds <= 0.5
        clock[0] += seconds
    with transport:
        result = RunCompanySyncRound(journal, producers, _SCOPE, CompanySyncBudget(None, None, None, float("inf")),
            monotonic=lambda: clock[0], cancel_requested=lambda: outcome == "cancelled" and clock[0] > 0,
            sleeper=sleep, transient_retry_seconds=5)
    family = result.families[0]
    if outcome == "recovered":
        assert result.business_complete and family.result.failure is None and not family.result.gaps
        assert len(physical) == family.source_calls == 3 and clock[0] == 5
    elif outcome == "repeated":
        assert result.business_complete and family.result.failure is None
        assert len(physical) == family.source_calls == 7 and clock[0] == 25
    elif outcome == "cancelled":
        assert result.cancelled and len(physical) == 1 and clock[0] == 0.5
    else:
        assert family.result.failure == "source_attempt_unconfirmed" and not family.result.can_continue
        assert family.unknown_attempts == 1 and physical == [] and clock[0] == 0


@pytest.mark.parametrize(("status", "retry"), ((403, False), (404, False), (408, True), (429, True), (503, True)))
def test_actual_http_status_controls_cooldown_and_only_same_cursor_clears_its_failure(tmp_path, status, retry):
    from trader.download.infra.company_http import CheckCompanyResponse
    journal, producers, _, physical, _, transport = _resources(tmp_path)
    producer = producers[0]
    original = producer.advance
    clock = [0.0]
    def fetch(scope, **kwargs):
        if not physical:
            response = transport.get("https://example.test/rejected", timeout=1)
            CheckCompanyResponse(response)
        return original(scope, **kwargs)
    def send(request):
        physical.append(request.url)
        return HttpResponse(status if len(physical) == 1 else 200, b"response")
    def sleep(delay):
        clock[0] += delay
    producer.advance = fetch
    transport._execute_request = send
    with transport:
        result = RunCompanySyncRound(journal, producers, _SCOPE, CompanySyncBudget(None, None, None, float("inf")),
            monotonic=lambda: clock[0], cancel_requested=lambda: False, sleeper=sleep, transient_retry_seconds=5)
    family = result.families[0]
    assert result.business_complete is retry and (family.result.failure is None) is retry
    assert len(physical) == (3 if retry else 1) and clock[0] == (5 if retry else 0)


def test_observed_old_transport_rejection_reopens_but_remains_distinct_from_an_unknown_attempt(tmp_path):
    journal, producers, budget, physical, _, transport = _resources(tmp_path)
    round_id = journal.begin(_SCOPE, tuple(item.plan for item in producers), max_source_calls=budget.max_source_calls)
    for producer in producers:
        family = producer.plan.family
        journal.start(round_id, family, producer.state(_SCOPE))
        if family is CompanySyncFamily.FINANCIAL:
            with transport:
                transport.get("https://example.test/prior", timeout=1)
            journal.finish(round_id, family, CompanySyncStepResult(False, False, failure="http_transport_error", gaps=("supplier_input_rejected",)))
        else:
            journal.finish(round_id, family, CompanySyncStepResult(True, False, pending_count=0))
    assert not journal.can_refresh(round_id)
    restarted, producers, budget, current_calls, _, transport = _resources(tmp_path)
    with transport:
        result = RunCompanySyncRound(restarted, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: False)
    assert result.business_complete and restarted.can_refresh(round_id)
    assert physical == ["https://example.test/prior"] and current_calls == ["https://example.test/1", "https://example.test/2"]


def test_capacity_block_resumes_in_same_round_after_storage_recovers(tmp_path):
    journal, producers, budget, physical, _, transport = _resources(tmp_path)
    announcement = next(p for p in producers if p.plan.family is CompanySyncFamily.ANNOUNCEMENTS)
    attempts = []

    def blocked(scope, *, cancel_requested, deadline):
        attempts.append(scope.identity)
        return CompanySyncStepResult(False, False, gaps=("company_raw_capacity_unavailable",))

    announcement.advance = blocked
    with transport:
        first = RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: False)
        again = RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: False)
    assert attempts == [_SCOPE.identity] * 2
    assert first.round_id == again.round_id and not journal.can_refresh(first.round_id)
    with pytest.raises(ValueError, match="requires_terminal_scope"):
        journal.finish_refresh(first.round_id)
    assert all(result.result.business_complete for result in again.families if result.family != announcement.plan.family)
    restarted, producers, budget, new_requests, calls, transport = _resources(tmp_path)
    with transport:
        recovered = RunCompanySyncRound(restarted, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: False)
    assert recovered.round_id == first.round_id and recovered.business_complete and restarted.can_refresh(first.round_id)
    assert calls == ["announcements"] and new_requests == [] and len(physical) == 2
    with SqliteSession(restarted.path) as conn:
        steps = conn.run("SELECT gaps FROM company_sync_step WHERE round_id=? AND family='announcements' ORDER BY step", (first.round_id,))
        assert [row[0] for row in steps] == ['["company_raw_capacity_unavailable"]'] * 2 + ["[]"]


def test_new_local_rules_and_input_watermark_reopen_only_fields_and_clear_resolved_gap(tmp_path):
    journal, producers, budget, physical, calls, transport = _resources(tmp_path)
    fields = producers[-1]
    fields.plan = replace(fields.plan, refresh_on_input_change=True)
    fields.state = lambda scope: CompanySyncPhaseState(CanonicalDigest((fields.plan, "old_material")), False)
    fields.advance = lambda scope, **kwargs: CompanySyncStepResult(True, False, pending_count=1,
        gaps=("field_proof_incomplete",), changed_codes=("600001",))
    with transport:
        first = RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: False)
        assert len(physical) == 2 and not first.business_complete
        fields.plan = replace(fields.plan, request_hash=CanonicalDigest("new_actual_field_rules"))
        state_checks = []

        def recovered_state(scope):
            state_checks.append(scope)
            return CompanySyncPhaseState(CanonicalDigest((fields.plan, "recovered_material")), False)

        def confirm_material(_scope, **_kwargs):
            assert state_checks == [_SCOPE]
            return CompanySyncStepResult(True, False, pending_count=0)

        fields.state = recovered_state
        fields.advance = confirm_material
        final = RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: False)
    assert final.round_id == first.round_id and len(physical) == 2 and final.business_complete
    assert final.families[-1].result.changed_codes == ("600001",)
    assert final.families[-1].result.pending_count == 0 and not final.families[-1].result.gaps
    assert journal.active().max_source_calls == budget.max_source_calls


@pytest.mark.parametrize("error_type", (OSError, PermissionError, FileNotFoundError))
def test_local_acceptance_failure_stops_every_later_family_and_keeps_request_intent(tmp_path, error_type):
    journal, producers, budget, physical, calls, transport = _resources(tmp_path)
    error = error_type("local_input_commit_failed")

    def failed_commit(*args):
        raise error

    producers[0].inputs.complete_task = failed_commit
    with transport, pytest.raises(error_type) as caught:
        RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: False)
    assert caught.value is error and len(physical) == 1 and not calls
    assert journal.outstanding_family(journal.active().round_id) is CompanySyncFamily.FINANCIAL


def test_request_progress_tracks_durable_retries_and_resumes_without_reset(tmp_path):
    path = tmp_path / "journal.sqlite3"
    family = CompanySyncFamily.FINANCIAL
    scope = replace(_SCOPE, families=(family,))
    plans = (CompanySyncPhasePlan(family, CanonicalDigest(family)),)
    progress: list[CompanyRequestProgress] = []
    policy = SqlitePolicyFixture()
    journal = SQLiteCompanySyncRoundJournal(
        path,
        sqlite_policy=policy,
        publication_identity=lambda: "",
        observe_requests=progress.append,
    )
    journal.initialize()
    round_id = journal.begin(scope, plans, max_source_calls=2)
    journal.start(round_id, family, CompanySyncPhaseState(CanonicalDigest("first-page"), True))
    assert journal.executing_step == (round_id, family, 1)
    request = HttpRequest("GET", "https://example.test/first-page", 1)
    journal.authorize_request(request)
    journal.observe_attempt(HttpAttempt("example.test", "GET", 0.1, 0, 503, "http_503"))
    journal.authorize_request(request)
    with pytest.raises(CompanySyncSourceBudgetExceeded):
        journal.authorize_request(request)
    journal.begin(scope, plans, max_source_calls=2)

    assert [(item.used, item.unknown) for item in progress] == [(0, 0), (1, 1), (1, 0), (2, 1)]
    assert all(item.round_id == round_id and item.limit == 2 for item in progress)
    resumed: list[CompanyRequestProgress] = []
    reopened = SQLiteCompanySyncRoundJournal(
        path,
        sqlite_policy=policy,
        publication_identity=lambda: "",
        observe_requests=resumed.append,
    )
    assert reopened.begin(scope, plans, max_source_calls=2) == round_id
    assert resumed == [CompanyRequestProgress(round_id, 2, 2, 1)]
    assert reopened.executing_step is None


def test_request_progress_keeps_an_observation_committed_at_deadline(tmp_path):
    path = tmp_path / "journal.sqlite3"
    family = CompanySyncFamily.FINANCIAL
    scope = replace(_SCOPE, families=(family,))
    plans = (CompanySyncPhasePlan(family, CanonicalDigest(family)),)
    clock = [0.0]
    progress: list[CompanyRequestProgress] = []

    def observe(snapshot: CompanyRequestProgress) -> None:
        progress.append(snapshot)
        if snapshot.used == 1 and snapshot.unknown == 0:
            clock[0] = 10.0

    journal = SQLiteCompanySyncRoundJournal(
        path,
        sqlite_policy=SqlitePolicyFixture(),
        publication_identity=lambda: "",
        io_budget=CompanySyncIoBudget(deadline=10, monotonic=lambda: clock[0], cancel_requested=lambda: False),
        observe_requests=observe,
    )
    journal.initialize()
    round_id = journal.begin(scope, plans, max_source_calls=1)
    journal.start(round_id, family, CompanySyncPhaseState(CanonicalDigest("first-page"), True))
    journal.authorize_request(HttpRequest("GET", "https://example.test/first-page", 1))
    with pytest.raises(CompanySyncInterrupted):
        journal.observe_attempt(HttpAttempt("example.test", "GET", 0.1, 1, 200, ""))

    assert progress[-1] == CompanyRequestProgress(round_id, 1, 1, 0)
    with SqliteSession(path, SqliteConnectionOptions(read_only=True)) as connection:
        assert tuple(connection.run("SELECT observed,status FROM company_sync_request").require_one()) == (1, 200)
