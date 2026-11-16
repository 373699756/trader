"""Company checkpoints preserve cancellation, actual attempts, and committed outcomes."""

from dataclasses import replace
from datetime import date, timedelta
from threading import Event

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company.company_annual_income import _contract, _financial, _Sources, _REPORTED
from trader.download.application.company_evidence import (
    CompanyEvidenceDependencies, _AnnouncementRun, _CompanyRunContext, _SyncState, _news, _publish_batch,
)
from trader.download.application.company_operations import CompanyOperations
from trader.download.application.company_publication import CompanyPublicationConflict
from trader.download.infra.company_publication_commit import _SQLiteCompanyPublication
from trader.download.application.company_report_inputs import (
    CompanyReportInputPlan, CollectCompanyReportInputs, PlanCompanyReportInputs,
)
from trader.download.application.company_structured import CollectCompanyStructured
from trader.download.domain.company_annual_income import CompanyAnnualIncomeRequest
from trader.download.domain.company_evidence import CompanyRatio, StructuredCompanyPage, StructuredCompanyQuery
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.infra.sqlite_connection import SqliteSession
from trader.infra.workers import BoundedExecutor


def _identity(index):
    with SqliteSession(index.facts_path) as connection:
        return ReadCompanyPublicationIdentity(connection)


def _state(dependencies, progress=None):
    return _SyncState(CompanyOperations(progress, dependencies.monotonic, ()), 1)


@pytest.mark.parametrize("stage", ("financial", "pledge", "unlock", "pledge_day"))
def test_late_structured_result_does_not_write_or_publish_after_cancellation(tmp_path, monkeypatch, stage):
    index = fixtures._index(tmp_path)
    contract = _contract(index)
    cancelled = Event()
    target = "pledge" if stage == "pledge_day" else stage
    with SqliteSession(index.path) as connection:
        connection.run("DELETE FROM component_checks WHERE component=?", (target,))
    cursor = index.structured_cursor

    def checked_cursor(query):
        assert not cancelled.is_set(), "a cancelled source result must not reopen a cursor"
        return cursor(query)

    monkeypatch.setattr(index, "structured_cursor", checked_cursor)

    class Source(_Sources):
        before_result = None

        def pledge_day(self, observed_at):
            if stage == "pledge_day":
                self.before_result = _identity(index)
                cancelled.set()
            return observed_at.date()

        def fetch(self, query, **kwargs):
            if isinstance(query, StructuredCompanyQuery) and query.component == stage:
                self.before_result = _identity(index)
                cancelled.set()
                return StructuredCompanyPage(
                    stage, 1, 1, 1,
                    financials=(_financial(basic_eps=2),) if stage == "financial" else (),
                    ratios=() if stage == "financial" else (CompanyRatio("600001", fixtures.NOW.date(), 10),),
                )
            return super().fetch(query, **kwargs)

    source = Source(contract)
    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0, cancel_requested=cancelled.is_set),
        contract.codes, fixtures.NOW,
    )
    assert cancelled.is_set() and result.failures == ("task:cancelled",)
    assert result.pending_partitions is result.pending_documents is None
    assert result.changed_rows == 0 and source.before_result is not None and _identity(index) == source.before_result
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT eps FROM financials WHERE code='600001'").require_one()[0] == 1
        assert connection.run("SELECT count(*) FROM ratios").require_one()[0] == 0
        assert connection.run("SELECT count(*) FROM component_checks WHERE component=?", (target,)).require_one()[0] == 0


@pytest.mark.parametrize(
    "stage", ("financial", "pledge", "unlock", "annual_income", "financial_reports", "news", "directory", "prefetch", "publication"),
)
def test_queued_cancellation_prevents_actual_callback_and_does_not_claim_an_input_attempt(tmp_path, monkeypatch, stage):
    index = fixtures._index(tmp_path)
    contract = _contract(index)
    cancelled = Event()
    observed = []
    calls = []
    source = _Sources(contract)
    kinds = {
        "financial": "structured", "pledge": "structured", "unlock": "structured",
        "annual_income": "annual_income", "financial_reports": "annual_income", "news": "news",
        "directory": "directory", "prefetch": "directory", "publication": "publication",
    }
    expected_source = "local" if stage == "publication" else source.news_source if stage == "news" else source.source

    def observe(event):
        if (event.operation is not None and event.operation.kind == kinds[stage]
            and event.operation.source == expected_source and event.operation.state == "queued"):
            observed.append(event.operation)
            cancelled.set()

    def forbidden(*args, **kwargs):
        calls.append(stage)
        pytest.fail("a queued cancellation must precede the actual source or publisher callback")

    for method in ("fetch", "pledge_day", "annual_income", "financial_reports", "news"):
        monkeypatch.setattr(source, method, forbidden)
    publish = index.publication
    if stage == "publication":
        monkeypatch.setattr(index, "publication", forbidden)
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0, observe, cancel_requested=cancelled.is_set)
    state = _state(dependencies, observe)
    state.published = 1
    executor = None
    announcements = None
    try:
        if stage in {"financial", "pledge", "unlock"}:
            with SqliteSession(index.path) as connection:
                connection.run("DELETE FROM component_checks WHERE component=?", (stage,))
            CollectCompanyStructured(dependencies, stage, fixtures.NOW, state)
        elif stage in {"annual_income", "financial_reports"}:
            plan = PlanCompanyReportInputs(dependencies, contract.codes, fixtures.NOW, state)
            if stage == "financial_reports":
                plan = replace(plan, annual_income=())
            CollectCompanyReportInputs(dependencies, plan, state)
        elif stage == "news":
            assert _news(dependencies, contract.codes, fixtures.NOW, state) == ()
        elif stage in {"directory", "prefetch"}:
            if stage == "prefetch":
                executor = BoundedExecutor(worker_count=1, queue_capacity=0, thread_name_prefix="company-queued-cancel")
                executor.start()
                dependencies = replace(dependencies, directory_executor=executor)
            context = _CompanyRunContext(fixtures.NOW, 0, fixtures._TEST_COMPANY_CONFIGURATION, fixtures.NOW.date())
            announcements = _AnnouncementRun(dependencies, state, context)
            announcements.prepare(contract.codes)
            announcements.run_batch(1)
        else:
            assert _publish_batch(dependencies, contract.codes, fixtures.NOW, 0, state) == 1
    finally:
        if announcements is not None:
            announcements.close()
        if executor is not None:
            executor.stop(wait=True, cancel_futures=True)
        monkeypatch.setattr(index, "publication", publish)
    assert observed and cancelled.is_set() and not calls
    assert state.annual_income_attempts == state.financial_report_attempts == set()
    assert state.failures == () and not state.operations._submitted


def test_cancel_after_structured_commit_keeps_real_counts_without_reopening_the_remaining_window(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    cancelled = Event()
    events = []
    first = _financial(published_at=fixtures.NOW - timedelta(days=3))
    query = StructuredCompanyQuery("financial", fixtures.NOW.date() - timedelta(days=365), fixtures.NOW.date() - timedelta(days=1))
    index.commit_structured(query, StructuredCompanyPage("financial", 1, 2, 2, financials=(first,)), fixtures.NOW)
    reads = []
    component_since = index.component_since

    def checked_since(*args):
        assert not cancelled.is_set(), "a committed cancelled page must not open the remaining source window"
        reads.append(args)
        return component_since(*args)

    monkeypatch.setattr(index, "component_since", checked_since)

    class Source(fixtures.Sources):
        def fetch(self, selected, **kwargs):
            assert selected.page == 2
            return StructuredCompanyPage(
                "financial", 2, 2, 2,
                financials=(_financial(code="600002", published_at=fixtures.NOW - timedelta(days=2)),),
            )

    def observe(event):
        events.append(event)
        if event.result is not None and event.result.component == "financial":
            cancelled.set()

    source = Source()
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0, observe, cancel_requested=cancelled.is_set)
    state = _state(dependencies, observe)
    CollectCompanyStructured(dependencies, "financial", fixtures.NOW, state)
    assert cancelled.is_set() and len(reads) == 1 and state.pages == state.changes == 1
    assert events[-1].status == "deferred" and not state.failures
    assert not any(event.component == "financial" and event.status == "complete" for event in events)
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT count(*) FROM financials").require_one()[0] == 2
        assert connection.run("SELECT count(*) FROM structured_progress").require_one()[0] == 0


@pytest.mark.parametrize("stage", ("financial", "annual_income", "financial_reports", "news"))
def test_queued_input_commit_cancellation_does_not_enter_the_business_writer(tmp_path, monkeypatch, stage):
    index = fixtures._index(tmp_path)
    contract = _contract(index)
    cancelled = Event()
    source_calls, writer_calls, events = [], [], []
    annual = fixtures._disclosure("queued-report", "2025年年度报告", when=_REPORTED)

    class Source(_Sources):
        def fetch(self, query, **kwargs):
            source_calls.append(stage)
            return StructuredCompanyPage("financial", 1, 1, 1, financials=(_financial(basic_eps=2),))

        def annual_income(self, request, *, observed_at):
            source_calls.append(stage)
            return super().annual_income(request, observed_at=observed_at)

        def financial_reports(self, request, *, observed_at):
            source_calls.append(stage)
            return (annual,)

        def news(self, code, *, observed_at):
            source_calls.append(stage)
            return (replace(fixtures._disclosure("queued-news"), source=self.news_source, kind="news"),)

    source = Source(contract)
    expected_source = source.news_source if stage == "news" else source.source

    def observe(event):
        events.append(event)
        if (event.operation is not None and event.operation.kind == "collection_commit"
            and event.operation.source == expected_source and event.operation.state == "queued"):
            cancelled.set()

    def forbidden(*args, **kwargs):
        writer_calls.append(stage)
        pytest.fail("cancellation before writer entry must not begin a new commit")

    method = "commit_structured" if stage == "financial" else f"commit_{stage}"
    monkeypatch.setattr(index, method, forbidden)
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0, observe, cancel_requested=cancelled.is_set)
    state = _state(dependencies, observe)
    before = _identity(index)
    if stage == "financial":
        with SqliteSession(index.path) as connection:
            connection.run("DELETE FROM component_checks WHERE component='financial'")
        CollectCompanyStructured(dependencies, stage, fixtures.NOW, state)
    elif stage == "news":
        assert _news(dependencies, contract.codes, fixtures.NOW, state) == contract.codes
    else:
        plan = PlanCompanyReportInputs(dependencies, contract.codes, fixtures.NOW, state)
        if stage == "financial_reports":
            plan = replace(plan, annual_income=())
        CollectCompanyReportInputs(dependencies, plan, state)
    assert cancelled.is_set() and source_calls == [stage] and not writer_calls
    assert state.changes == 0 and state.failures == () and _identity(index) == before
    assert not any(event.result is not None and event.result.outcome == "accepted" for event in events)


@pytest.mark.parametrize("phase", ("queued", "completed", "result"))
@pytest.mark.parametrize("code", ("company_publication_source_changed", "company_publication_parent_changed"))
def test_publication_observation_with_a_conflict_code_is_never_retried(tmp_path, monkeypatch, phase, code):
    index = fixtures._index(tmp_path)
    contract = _contract(index)
    source = fixtures.Sources()
    calls = []
    events = []
    original = RuntimeError(code)
    publish = index.publication

    def publisher(*args, **kwargs):
        calls.append(fixtures.NOW)
        return publish(*args, **kwargs)

    def observe(event):
        events.append(event)
        if phase == "result" and event.result is not None and event.result.component == "publish":
            raise original
        if event.operation is not None and event.operation.kind == "publication" and event.operation.state == phase:
            raise original

    monkeypatch.setattr(index, "publication", publisher)
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0, observe)
    state = _state(dependencies, observe)
    with pytest.raises(RuntimeError) as raised:
        _publish_batch(dependencies, contract.codes, fixtures.NOW, 0, state)
    assert raised.value is original and len(calls) == int(phase != "queued")
    assert not any(event.recovery is not None and event.recovery.decision == "retry" for event in events)


@pytest.mark.parametrize("outcome", ("recovered", "exhausted", "cancelled", "cancelled_final"))
def test_only_actual_publication_conflicts_retry_with_the_original_bound_and_cancel_gate(tmp_path, monkeypatch, outcome):
    index = fixtures._index(tmp_path)
    contract = _contract(index)
    source = fixtures.Sources()
    cancelled = Event()
    before = _identity(index)
    commit = _SQLiteCompanyPublication.commit
    calls = []
    original = CompanyPublicationConflict("company_publication_parent_changed")
    with SqliteSession(index.path) as connection:
        connection.run("INSERT OR IGNORE INTO company_dirty_codes VALUES('600001')")

    def publisher(session):
        calls.append(fixtures.NOW)
        if outcome == "cancelled" or outcome == "cancelled_final" and len(calls) == 3:
            cancelled.set()
        if len(calls) == 1 or outcome in {"exhausted", "cancelled_final"}:
            raise original
        return commit(session)

    monkeypatch.setattr(_SQLiteCompanyPublication, "commit", publisher)
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0, cancel_requested=cancelled.is_set)
    state = _state(dependencies)
    state.published = 1
    if outcome == "exhausted":
        with pytest.raises(RuntimeError) as raised:
            _publish_batch(dependencies, contract.codes, fixtures.NOW, 0, state)
        assert raised.value is original and len(calls) == 3
    else:
        assert _publish_batch(dependencies, contract.codes, fixtures.NOW, 0, state) == 1
        assert len(calls) == (1 if outcome == "cancelled" else 3 if outcome == "cancelled_final" else 2)
    assert _identity(index) == before


@pytest.mark.parametrize("checkpoints", (False, True))
def test_annual_input_attempts_remain_unique_when_new_needs_regroup_the_same_periods(tmp_path, monkeypatch, checkpoints):
    index = fixtures._index(tmp_path)
    days = tuple(date(year, 12, 31) for year in (2023, 2024, 2025))
    first = CompanyAnnualIncomeRequest("600001", days[:2])
    current = CompanyReportInputPlan(
        fixtures.NOW, (first,) if checkpoints else (first, CompanyAnnualIncomeRequest("600001", days[1:])), (),
    )

    class Source(fixtures.Sources):
        requests = ()

        def annual_income(self, request, *, observed_at):
            assert observed_at == fixtures.NOW
            assert all((request.code, day) in state.annual_income_attempts for day in request.report_dates)
            self.requests += (request,)
            raise TimeoutError("fixture_source_timeout")

    source = Source()
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0)
    state = _state(dependencies)
    monkeypatch.setattr(index, "report_input_plan", lambda *args: current)
    plan = PlanCompanyReportInputs(dependencies, ("600001",), fixtures.NOW, state)
    CollectCompanyReportInputs(dependencies, plan, state)
    if checkpoints:
        current = replace(current, annual_income=(CompanyAnnualIncomeRequest("600001", days),))
        plan = PlanCompanyReportInputs(dependencies, ("600001",), fixtures.NOW, state)
        assert plan.annual_income == (CompanyAnnualIncomeRequest("600001", days[2:]),)
        CollectCompanyReportInputs(dependencies, plan, state)
    assert source.requests == (first, CompanyAnnualIncomeRequest("600001", days[2:]))
    assert state.annual_income_attempts == {("600001", day) for day in days}
    assert PlanCompanyReportInputs(dependencies, ("600001",), fixtures.NOW, state).annual_income == ()


@pytest.mark.parametrize("stage", ("annual_income", "financial_reports"))
def test_cancel_after_input_commit_reports_the_actual_rows_without_publishing_them(tmp_path, stage):
    index = fixtures._index(tmp_path)
    contract = _contract(index)
    cancelled = Event()
    annual = fixtures._disclosure("new-input-report", "2025年年度报告", when=_REPORTED)

    class Source(_Sources):
        phase = ""
        before_input = None

        def annual_income(self, request, *, observed_at):
            self.phase = "annual_income"
            self.before_input = _identity(index)
            return super().annual_income(request, observed_at=observed_at)

        def financial_reports(self, request, *, observed_at):
            self.phase = "financial_reports"
            return (annual,)

    source = Source(contract)

    def observe(event):
        if (source.phase == stage and event.operation is not None
            and event.operation.kind == "collection_commit" and event.operation.state == "completed"):
            cancelled.set()

    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0, observe, cancel_requested=cancelled.is_set),
        contract.codes, fixtures.NOW,
    )
    assert cancelled.is_set() and result.failures == ("task:cancelled",)
    assert result.changed_rows == (1 if stage == "annual_income" else 2)
    assert result.pending_partitions is result.pending_documents is None
    assert source.before_input is not None and _identity(index) == source.before_input
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT count(*) FROM annual_income").require_one()[0] == 1
        assert connection.run("SELECT count(*) FROM disclosures WHERE source_key=?", (annual.identity,)).require_one()[0] == int(stage == "financial_reports")
