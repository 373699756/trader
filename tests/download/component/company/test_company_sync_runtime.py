"""The real current-schema resources run all families without a global source guard."""

from datetime import timedelta

import pytest

from tests.download.component.company.test_company_sync_round import _SCOPE
from tests.helpers.download.configuration import CompanyEventPolicyFixture
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.domain.company_evidence import CompanyDatabaseIncompatibleError, CompanyDownloadStore, DisclosurePage, StructuredCompanyPage
from trader.download.domain.company_source_records import CompanyFinancialSourcePage
from trader.download.domain.company_sync import CompanySyncBudget
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_facts_index import SQLiteCompanyFactsIndex
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.download.infra.company_financial_observations import ReadCompanyFinancialObservations
from trader.download.infra.company_material_index import SQLiteCompanyMaterialIndex
from trader.download.infra.company_normalized_index import SQLiteCompanyNormalizedIndex
from trader.download.infra.company_sync_inputs import SQLiteCompanySyncInputStore
from trader.download.infra.company_sync_journal import SQLiteCompanySyncRoundJournal
from trader.download.infra.company_sync_runtime import CompanySyncRuntime
from trader.infra.http_contracts import HttpResponse
from trader.infra.http_transport import RequestsHttpTransport
from trader.infra.sqlite_connection import SqliteSession


class _Sources:
    def __init__(self, transport):
        self.transport = transport

    def financial_observations(self, query, *, observed_at):
        self.transport.get("https://example.test/financial", timeout=1)
        return CompanyFinancialSourcePage(1, 1, 0, 0, (), "a" * 64)

    def fetch(self, query, *, observed_at=None, order=""):
        self.transport.get("https://example.test/page", timeout=1)
        return StructuredCompanyPage(query.component, 1, 1, 0) if observed_at is not None else DisclosurePage((), 0, False)

    def pledge_day(self, observed_at):
        return observed_at.date()

    def annual_income(self, request, *, observed_at):
        raise AssertionError("no_applicable_contract_must_not_fetch_income")

    def news(self, code, *, observed_at):
        self.transport.get("https://example.test/news", timeout=1)
        return ()

    def document_ready(self, item):
        raise AssertionError("no_exact_missing_dependency_must_not_plan_pdf")

    def download_document(self, item, *, attempt_limit=3):
        raise AssertionError("no_pdf_fanout")

    def extract_document(self, item, **kwargs):
        raise AssertionError("no_pdf_fanout")


def _runtime(tmp_path):
    policy, event_policy = SqlitePolicyFixture(), CompanyEventPolicyFixture()
    facts = SQLiteCompanyFactsIndex(tmp_path / "formal.sqlite3", event_policy=event_policy, sqlite_policy=policy)
    reader = SQLiteCompanyEvidenceReader(facts.path, event_policy=event_policy, sqlite_policy=policy)
    documents = CompanyDocumentFiles(tmp_path / "raw/text", max_text_characters=100000, max_bytes=1000000, reserve_bytes=1)
    material = SQLiteCompanyMaterialIndex(tmp_path / "raw/company_collection.sqlite3", facts=facts, documents=documents,
        event_policy=event_policy, sqlite_policy=policy)
    normalized = SQLiteCompanyNormalizedIndex(tmp_path / "normalized.sqlite3", facts, event_policy=event_policy,
        sqlite_policy=policy, clean_material=lambda: None)
    journal = SQLiteCompanySyncRoundJournal(tmp_path / "journal.sqlite3", sqlite_policy=policy, publication_identity=reader.publication_identity)
    inputs = SQLiteCompanySyncInputStore(tmp_path / "inputs.sqlite3", sqlite_policy=policy)
    physical = []
    def execute(request):
        physical.append(request.url)
        return HttpResponse(200, b"ok")
    transport = RequestsHttpTransport(authorize=journal.authorize_request, observe=journal.observe_attempt, execute_request=execute)
    source = _Sources(transport)
    runtime = CompanySyncRuntime(normalized, facts, material, journal, inputs, event_policy=event_policy, sqlite_policy=policy,
        read_document=documents.read, financial_source=source, ratio_source=source, annual_source=source,
        news_source=source, official_source=source)
    return runtime, reader, transport, physical


def test_real_empty_target_initializes_unavailable_and_runs_finite_families_with_explicit_source_gaps(tmp_path):
    runtime, reader, transport, physical = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    initial, = reader.read(_SCOPE.codes)
    assert initial.financial is None and initial.financial_state.value == "unavailable"
    with transport:
        result = runtime.run(_SCOPE.codes, _SCOPE.cutoff, CompanySyncBudget(100, 20, 20, 10),
            cancel_requested=lambda: False, monotonic=lambda: 0)
    assert result.processing_complete and not result.business_complete and not result.cancelled
    assert len(result.families) == 7 and len(physical) == sum(item.source_calls for item in result.families)
    assert result.publication_hash == reader.publication_identity()
    assert reader.read(_SCOPE.codes) == (initial,)
    assert runtime.journal.can_refresh(result.round_id)


def test_unfinished_round_resumes_original_time_and_does_not_refill_request_allowance(tmp_path):
    runtime, reader, transport, physical = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    with transport:
        first = runtime.run(_SCOPE.codes, _SCOPE.cutoff, CompanySyncBudget(1, 20, 20, 10), cancel_requested=lambda: False, monotonic=lambda: 0)
        assert first.paused and not physical
        result = runtime.run(_SCOPE.codes, _SCOPE.cutoff + timedelta(days=1), CompanySyncBudget(100, 200, 20, 10),
            cancel_requested=lambda: False, monotonic=lambda: 0)
    assert result.processing_complete and result.scope == _SCOPE
    assert runtime.journal.active().max_source_calls == 20


def test_old_formal_schema_rejected_before_other_stores_or_requests_are_created(tmp_path):
    runtime, reader, transport, physical = _runtime(tmp_path)
    with SqliteSession(runtime.facts.path) as connection:
        connection.run("CREATE TABLE obsolete(value TEXT)")
    with pytest.raises(CompanyDatabaseIncompatibleError) as error:
        runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    assert error.value.store is CompanyDownloadStore.FACTS
    assert not runtime.material.path.exists() and not runtime.inputs.path.exists() and not runtime.journal.path.exists()
    assert not physical
    transport.close()


def test_unadmitted_complete_financial_row_stays_observation_and_never_leaks_through_field_publication(tmp_path):
    from datetime import date
    from trader.download.domain.company_evidence import CompanyFinancial
    from trader.download.domain.company_source_records import CompanyFinancialSourceObservation, CompanyFinancialSourceRecordHash
    runtime, reader, transport, physical = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    financial = CompanyFinancial("600001", date(2026, 9, 30), _SCOPE.cutoff - timedelta(days=1), 1, 2, 3, 4, 5, 6, 7, 8)
    observation = CompanyFinancialSourceObservation(financial, "600001.SH", financial.published_at, financial.published_at,
        _SCOPE.cutoff, "CNY", CompanyFinancialSourceRecordHash(financial, "600001.SH", financial.published_at, financial.published_at, "CNY"), ())
    def source(query, *, observed_at):
        runtime._financial_source.transport.get("https://example.test/financial", timeout=1)
        return CompanyFinancialSourcePage(1, 1, 1, 1, (observation,), "a" * 64)
    runtime._financial_source.financial_observations = source
    before = reader.read(_SCOPE.codes)
    with transport:
        result = runtime.run(_SCOPE.codes, _SCOPE.cutoff, CompanySyncBudget(100, 20, 20, 10), cancel_requested=lambda: False, monotonic=lambda: 0)
    assert result.processing_complete and not result.business_complete and reader.read(_SCOPE.codes) == before
    with SqliteSession(runtime.normalized.path) as normalized, SqliteSession(runtime.facts.path) as formal:
        assert ReadCompanyFinancialObservations(normalized, _SCOPE.codes) == (observation,)
        assert ReadCompanyFinancialObservations(formal, _SCOPE.codes) == ()
        assert formal.run("SELECT COUNT(*) FROM company_input_receipt WHERE source='eastmoney_financial'").require_one()[0] == 0


def test_same_scope_manual_run_and_fresh_runtime_are_idempotent_without_source_or_head_changes(tmp_path):
    runtime, reader, transport, physical = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    with transport:
        first = runtime.run(_SCOPE.codes, _SCOPE.cutoff, CompanySyncBudget(100, 20, 20, 10), cancel_requested=lambda: False, monotonic=lambda: 0)
    runtime, reader, transport, physical = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    with transport:
        resumed = runtime.run(_SCOPE.codes, _SCOPE.cutoff, CompanySyncBudget(100, 20, 20, 10), cancel_requested=lambda: False, monotonic=lambda: 0)
    assert resumed.round_id == first.round_id and resumed.publication_hash == first.publication_hash and not physical


def test_preflight_ack_before_marker_failure_resumes_local_work_without_blocking_source(tmp_path, monkeypatch):
    runtime, reader, transport, physical = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    original = runtime.inputs.complete_task
    def failed(scope, family, task, accepted, input_hash):
        if family == "fields" and task.startswith("preflight:"):
            raise RuntimeError("preflight_marker_failure")
        return original(scope, family, task, accepted, input_hash)
    monkeypatch.setattr(runtime.inputs, "complete_task", failed)
    with transport, pytest.raises(RuntimeError, match="preflight_marker_failure"):
        runtime.run(_SCOPE.codes, _SCOPE.cutoff, CompanySyncBudget(100, 20, 20, 10), cancel_requested=lambda: False, monotonic=lambda: 0)
    assert not physical
    runtime, reader, transport, physical = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    with transport:
        result = runtime.run(_SCOPE.codes, _SCOPE.cutoff, CompanySyncBudget(100, 20, 20, 10), cancel_requested=lambda: False, monotonic=lambda: 0)
    assert result.processing_complete and physical


def test_corrupt_formal_payload_is_rejected_before_new_store_or_budget_writes(tmp_path):
    from trader.infra.sqlite_publication import SqlitePublicationError
    runtime, reader, transport, physical = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    head = reader.publication_identity()
    journal_before = runtime.journal.path.read_bytes()
    with SqliteSession(runtime.facts.path) as formal:
        formal.run("UPDATE company_facts SET pending_docs=99 WHERE code='600001'")
    with pytest.raises(SqlitePublicationError):
        runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    assert reader.publication_identity() == head
    assert runtime.journal.path.read_bytes() == journal_before and not physical
    transport.close()


def test_resume_filters_newly_ineligible_members_without_losing_fixed_scope_or_facts(tmp_path):
    runtime, reader, transport, physical = _runtime(tmp_path)
    codes = ("600001", "600002")
    runtime.initialize(codes, _SCOPE.cutoff)
    before = reader.read(codes)
    with transport:
        first = runtime.run(codes, _SCOPE.cutoff, CompanySyncBudget(1, 20, 20, 10),
            cancel_requested=lambda: False, monotonic=lambda: 0)
        assert first.paused and not physical
        result = runtime.run(("600001",), _SCOPE.cutoff + timedelta(days=1), CompanySyncBudget(100, 200, 20, 10),
            cancel_requested=lambda: False, monotonic=lambda: 0)
    assert result.scope.codes == codes and result.scope.cutoff == _SCOPE.cutoff
    assert runtime.journal.active().max_source_calls == 20
    assert reader.read(codes) == before and result.processing_complete
    assert all("population_members_became_ineligible" in item.result.gaps for item in result.families)


@pytest.mark.parametrize("cancelled", (False, True))
def test_entry_budget_or_cancel_stops_before_any_new_sql_or_http(tmp_path, cancelled):
    runtime, reader, transport, physical = _runtime(tmp_path)
    result = runtime.run(_SCOPE.codes, _SCOPE.cutoff, CompanySyncBudget(100, 20, 20, 0),
        cancel_requested=lambda: cancelled, monotonic=lambda: 0)
    assert result.cancelled is cancelled and result.budget_exhausted is not cancelled
    assert not result.families and not result.publication_hash and not result.round_id
    assert not runtime.facts.path.exists() and not runtime.journal.path.exists() and not physical
    transport.close()
