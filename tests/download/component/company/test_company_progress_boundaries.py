"""Progress follows committed work without extending the download plan."""

from collections import deque
from datetime import timedelta

import pytest

from tests.helpers.download.company.company_evidence import NOW, Sources, _TEST_COMPANY_CONFIGURATION, _index, _sync_company_evidence
from trader.download.application.company_evidence import (
    CompanyEvidenceDependencies,
    _AnnouncementRun,
    _CompanyRunContext,
    _SyncState,
)
from trader.download.application.company_operations import CompanyOperations
from trader.download.application.company_progress import ReportCompanyProgress
from trader.download.application.company_structured import CollectCompanyStructured
from trader.download.domain.company_events import CompanyDataState
from trader.download.domain.company_evidence import (
    CompanyFinancial,
    CompanyOperationProgress,
    CompanyRatio,
    CompanyStockProgress,
    DisclosurePartition,
    StructuredCompanyPage,
    StructuredCompanyQuery,
)
from trader.download.domain.company_facts import CompanyFacts
from trader.download.infra.company_publication_progress import ReportCompanyVerification


def _financial(code, disclosed):
    return CompanyFinancial(code, NOW.date() - timedelta(days=30), disclosed, 1, 2, 3, 4, 5, 6, 7, 8)


@pytest.mark.parametrize("fail_second", (False, True))
def test_structured_detail_follows_commit_and_filters_only_progress_population(tmp_path, monkeypatch, fail_second):
    events = []
    index = _index(tmp_path)
    financials = (
        _financial("600001", NOW - timedelta(days=2)),
        _financial("600002", NOW - timedelta(days=1)),
    )

    class Source(Sources):
        def fetch(self, query, *, observed_at):
            return StructuredCompanyPage("financial", query.page, 2, 2, financials=(financials[query.page - 1],))

    source = Source()
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0, events.append)
    state = _SyncState(CompanyOperations(events.append, lambda: 0, ()), 1, population_codes=frozenset({"600001"}))
    commit = index.commit_structured

    def commit_page(query, page, observed_at):
        persisted = [event.structured for event in events if event.structured is not None and event.structured.page_rows]
        assert len(persisted) == page.page - 1
        if fail_second and page.page == 2:
            raise RuntimeError("fixture_commit_failed")
        return commit(query, page, observed_at)

    monkeypatch.setattr(index, "commit_structured", commit_page)
    CollectCompanyStructured(dependencies, "financial", NOW, state)
    persisted = [event.structured for event in events if event.structured is not None and event.structured.page_rows]
    # The final component event repeats the last committed page, not another page commit.
    if not fail_second:
        assert persisted[-1] == persisted[-2]
        persisted.pop()
    assert len(persisted) == (1 if fail_second else 2)
    first = persisted[0]
    assert first.persisted_rows == first.page_rows == 1
    assert first.qualified_codes == ("600001",)
    assert first.first_date == first.last_date == financials[0].published_at.date()
    assert all(event.completion_scope == "component" for event in events if event.component == "financial")
    if not fail_second:
        assert persisted[1].qualified_codes == () and persisted[1].persisted_rows == 2
        assert events[-1].status == "complete"
    else:
        assert events[-1].status == "deferred" and events[-1].structured.page_rows == 0
        assert events[-1].structured.persisted_rows == 1


def test_directory_dates_keep_each_market_and_deferred_ranges_separate(tmp_path):
    events = []
    index, source = _index(tmp_path), Sources()
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0, events.append)
    state = _SyncState(CompanyOperations(events.append, lambda: 0, ()), 2)
    context = _CompanyRunContext(NOW, 0, _TEST_COMPANY_CONFIGURATION, NOW.date() - timedelta(days=365))
    run = _AnnouncementRun(dependencies, state, context)
    run._markets = ("sse", "szse")
    run._queue = deque((
        DisclosurePartition("sse", NOW.date() - timedelta(days=3), NOW.date()),
        DisclosurePartition("szse", NOW.date() - timedelta(days=1), NOW.date()),
    ))
    run._report()
    assert [(market, dates.completed_days, dates.total_days) for market, dates in events[-1].market_dates] == [
        ("sse", 362, 366), ("szse", 364, 366),
    ]
    run._queue.clear()
    run._deferred.add(("sse", NOW.date(), NOW.date(), ""))
    run._report(status="deferred")
    assert [(market, dates.completed_days) for market, dates in events[-1].market_dates] == [("sse", 365), ("szse", 366)]
    assert events[-1].dates.completed_days == 365


@pytest.mark.parametrize("count_changed", (False, True))
def test_structured_resume_and_restart_describe_current_query_not_old_coverage(tmp_path, count_changed):
    events = []
    index = _index(tmp_path)
    query = StructuredCompanyQuery("financial", NOW.date() - timedelta(days=365), NOW.date())
    index.commit_structured(query, StructuredCompanyPage(
        "financial", 1, 2, 2, financials=(_financial("600002", NOW - timedelta(days=2)),),
    ), NOW)

    class Source(Sources):
        def fetch(self, selected, *, observed_at):
            assert selected.page == 2 and selected.received_rows == 1
            return StructuredCompanyPage("financial", 2, 2, 3 if count_changed else 2,
                                         financials=(_financial("600001", NOW - timedelta(days=1)),))

    source = Source()
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0, events.append)
    state = _SyncState(CompanyOperations(events.append, lambda: 0, ()), 1, population_codes=frozenset({"600001"}))
    CollectCompanyStructured(dependencies, "financial", NOW, state)
    reports = [event for event in events if event.structured is not None]
    assert reports[0].structured.resumed and reports[0].structured.persisted_rows == 1
    final = reports[-1]
    if count_changed:
        assert final.status == "deferred" and final.structured.restarted
        assert final.structured.query.page == 1 and final.structured.persisted_rows == final.completed == 0
        assert final.pending is None and final.structured.page_rows == 0
    else:
        assert final.status == "complete" and final.structured.persisted_rows == 2


def test_existing_publication_baseline_and_finish_use_real_boundaries(tmp_path, monkeypatch):
    events = []
    index, source = _index(tmp_path), Sources()
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0, events.append)
    finish = index.finish
    inventory = index.document_workload

    def workload(*args, **kwargs):
        if not any(event.component == "resume" for event in events):
            preparation = [event for event in events if event.component == "prepare"]
            assert preparation[-1].status == "running"
            assert events[-1].operation.kind == "inventory" and events[-1].operation.state == "running"
        return inventory(*args, **kwargs)

    def finish_index(observed_at):
        reports = [event for event in events if event.component == "finish"]
        assert len(reports) == 1 and reports[0].status == "running"
        finish(observed_at)

    monkeypatch.setattr(index, "finish", finish_index)
    monkeypatch.setattr(index, "document_workload", workload)
    for expected in (0, 1):
        events.clear()
        result = _sync_company_evidence(dependencies, ("600001",), NOW)
        assert result.published_codes == 1
        baseline = [event for event in events if event.component in {"resume", "complete"}]
        assert len(baseline) == 2 and all(event.existing_published_codes == expected for event in baseline)
        assert all(event.existing_published_codes is None for event in events if event.component not in {"resume", "complete"})
        assert [event.status for event in events if event.component == "finish"] == ["running", "complete"]


@pytest.mark.parametrize("component", ("risk_documents", "facts", "publish", "annual_income"))
def test_local_batch_completion_is_not_component_completion(tmp_path, component):
    events = []
    index, source = _index(tmp_path), Sources()
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0, events.append)
    state = _SyncState(CompanyOperations(events.append, lambda: 0, ()), 1)
    ReportCompanyProgress(dependencies, component, state, 0, completed=1, status="complete")
    assert events[-1].status == "complete" and events[-1].completion_scope == "batch"
    ReportCompanyProgress(dependencies, "complete", state, 0, completed=1, status="complete")
    assert events[-1].completion_scope == "component"


def test_stock_verification_projects_real_component_states_and_dates():
    events = []
    financial = _financial("600001", NOW - timedelta(days=1))
    facts = CompanyFacts(
        "600001", financial, CompanyDataState.PARTIAL, CompanyRatio("600001", NOW.date(), 2),
        CompanyDataState.COMPLETE, (), NOW, NOW.date() + timedelta(days=180), CompanyDataState.COMPLETE,
        (), (), (), 0, NOW.date() - timedelta(days=365), NOW.date(), CompanyDataState.UNAVAILABLE, NOW, NOW,
        latest_financial=financial,
    )
    ReportCompanyVerification(events.append, facts, 1, 1, 2, 1, False)
    event = events[0]
    assert isinstance(event, CompanyStockProgress) and not event.published and not event.coverage_complete
    assert (event.financial_state, event.pledge_state, event.unlock_state, event.announcement_state) == (
        CompanyDataState.PARTIAL, CompanyDataState.COMPLETE, CompanyDataState.COMPLETE, CompanyDataState.UNAVAILABLE,
    )
    assert event.financial_report_date == financial.report_date
    assert event.financial_disclosed_at == financial.published_at
    assert event.pledge_date == NOW.date() and event.unlock_coverage_end == facts.unlock_coverage_end


@pytest.mark.parametrize("counts", ((None, 3, "pages"), (2, 1, "pages"), (True, 2, "pages"), (1, 2, None)))
def test_operation_units_reject_partial_or_invalid_counts(counts):
    with pytest.raises(ValueError, match="operation_units"):
        CompanyOperationProgress(1, "ocr", "running", 0, completed_units=counts[0], total_units=counts[1], unit=counts[2])


@pytest.mark.parametrize("codes", (("600001", "600001"), ("600002", "600001"), ("invalid",)))
def test_operation_related_codes_require_ordered_unique_stock_codes(codes):
    with pytest.raises(ValueError):
        CompanyOperationProgress(1, "download", "completed", 0, related_codes=codes)
