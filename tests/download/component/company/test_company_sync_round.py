"""Persisted scope and transport slots protect retries across process restarts."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.application.company_sync import RunCompanySyncRound
from trader.download.domain.company_sync import (
    CompanySyncBudget, CompanySyncFamily, CompanySyncPhasePlan, CompanySyncPhaseState, CompanySyncRoundScope, CompanySyncStepResult, CompanySyncInterrupted,
)
from trader.download.infra.company_sync_inputs import SQLiteCompanySyncInputStore
from trader.download.infra.company_sync_journal import SQLiteCompanySyncRoundJournal
from trader.infra.http_contracts import HttpResponse
from trader.download.domain.company_sync import CompanySourceCancelled
from trader.infra.http_transport import RequestsHttpTransport
from trader.infra.serialization.canonical_codec import CanonicalDigest


_SCOPE = CompanySyncRoundScope(("600001",), datetime(2026, 11, 17, tzinfo=ZoneInfo("Asia/Shanghai")))


class _Producer:
    def __init__(self, family, inputs, transport, calls, *, crash=False, cancel=None):
        self.plan = CompanySyncPhasePlan(family, CanonicalDigest(family))
        self.inputs, self.transport, self.calls = inputs, transport, calls
        self.crash, self.cancel = crash, cancel

    def _cursor(self):
        return next((page for page in (1, 2) if not self.inputs.task_done(_SCOPE.identity, self.plan.family.value, str(page))), None)

    def state(self, scope):
        page = self._cursor() if self.plan.family is CompanySyncFamily.FINANCIAL else None
        return CompanySyncPhaseState(CanonicalDigest((self.plan.family, page)), page is not None)

    def advance(self, scope, *, cancel_requested, deadline):
        page = self._cursor() if self.plan.family is CompanySyncFamily.FINANCIAL else None
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
    transport = RequestsHttpTransport(authorize=journal.authorize_request, observe=journal.observe_attempt, execute_request=execute)
    producers = tuple(_Producer(family, inputs, transport, calls, crash=crash and family is CompanySyncFamily.FINANCIAL,
        cancel=cancel if family is CompanySyncFamily.FINANCIAL else None) for family in CompanySyncFamily)
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


def test_unknown_response_acceptance_never_repeats_source_but_later_local_families_finish(tmp_path):
    cancelled = [False]
    journal, producers, budget, physical, calls, transport = _resources(tmp_path, cancel=cancelled)
    with transport:
        result = RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: cancelled[0])
    assert result.cancelled and len(physical) == 1
    journal, producers, budget, physical, calls, transport = _resources(tmp_path)
    with transport:
        result = RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: False)
    assert result.processing_complete and not result.business_complete and not physical
    assert result.families[0].result.failure == "source_attempt_unconfirmed"
    assert "fields" in calls
    with pytest.raises(ValueError, match="requires_terminal_scope"):
        journal.finish_refresh(result.round_id)


def test_fixed_source_budget_exhaustion_still_runs_local_fields_and_does_not_refill_on_resume(tmp_path):
    journal, producers, budget, physical, calls, transport = _resources(tmp_path, maximum=1)
    with transport:
        result = RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: False)
    assert result.budget_exhausted and result.processing_complete and not result.business_complete
    assert len(physical) == 1 and "fields" in calls
    journal, producers, budget, physical, calls, transport = _resources(tmp_path, maximum=1)
    with transport:
        result = RunCompanySyncRound(journal, producers, _SCOPE, budget, monotonic=lambda: 0, cancel_requested=lambda: False)
    assert not physical and journal.source_calls(result.round_id) == 1


def test_transport_rejection_and_unobserved_slot_cannot_be_reported_as_confirmed_http(tmp_path):
    journal, producers, budget, physical, calls, transport = _resources(tmp_path, maximum=1)
    plans = tuple(item.plan for item in producers)
    round_id = journal.begin(_SCOPE, plans, max_source_calls=1)
    journal.start(round_id, CompanySyncFamily.FINANCIAL, producers[0].state(_SCOPE))
    from trader.infra.http_contracts import HttpRequest
    journal.authorize_request(HttpRequest("GET", "https://example.test/1", 1))
    journal.finish(round_id, CompanySyncFamily.FINANCIAL, CompanySyncStepResult(False, False, failure="source_attempt_unconfirmed"))
    assert journal.family(round_id, CompanySyncFamily.FINANCIAL).unknown_attempts == 1
    assert physical == []
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


def test_new_local_rules_and_input_watermark_reopen_only_fields_and_clear_resolved_gap(tmp_path):
    from dataclasses import replace
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
        fields.state = lambda scope: CompanySyncPhaseState(CanonicalDigest((fields.plan, "recovered_material")), False)
        fields.advance = lambda scope, **kwargs: CompanySyncStepResult(True, False, pending_count=0)
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
