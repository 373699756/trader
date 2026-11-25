"""The real current-schema resources run all families without a global source guard."""

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.download.component.company.test_company_sync_round import _SCOPE
from tests.helpers.download.configuration import CompanyEventPolicyFixture
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.domain.company_evidence import CompanyDatabaseIncompatibleError, CompanyDownloadStore, CompanyInputValidationFailure, Disclosure, DisclosurePage, DisclosurePartition, DisclosureQuery, StructuredCompanyPage, StructuredCompanyQuery
from trader.download.domain.company_financial_admission import CompanyFinancialRevisionProof
from trader.download.domain.company_source_records import CompanyFinancialSourcePage
from trader.download.domain.company_sync import CompanySyncBudget, CompanySyncFamily, CompanySyncPhasePlan, CompanySyncPhaseState, CompanySyncRebuildActiveRound, CompanySyncStepResult
from trader.download.infra.company_disclosure_source import CNINFO_DIRECTORY_URL
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_facts_index import SQLiteCompanyFactsIndex
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.download.infra.company_financial_observations import ReadCompanyFinancialObservations
from trader.download.infra.company_material_index import SQLiteCompanyMaterialIndex
from trader.download.infra.company_normalized_index import SQLiteCompanyNormalizedIndex
from trader.download.infra.company_sync_inputs import CompanyRecoveredInput, SQLiteCompanySyncInputStore
from trader.download.infra.company_sync_journal import SQLiteCompanySyncRoundJournal
from trader.download.infra.company_sync_runtime import CompanySyncRuntime
from trader.download.infra.company_sync_producers import CompanyAnnouncementProducer
from trader.infra.http_contracts import HttpRequest, HttpResponse
from trader.infra.http_transport import RequestsHttpTransport
from trader.infra.serialization.canonical_codec import CanonicalDigest
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

    def document_ready(self, item):
        raise AssertionError("no_exact_missing_dependency_must_not_plan_pdf")

    def download_document(self, item, *, attempt_limit=3):
        raise AssertionError("no_pdf_fanout")

    def extract_document(self, item, **kwargs):
        raise AssertionError("no_pdf_fanout")


def _runtime(tmp_path, *, observe_scope=None, financial_revision_proofs=()):
    policy, event_policy = SqlitePolicyFixture(), CompanyEventPolicyFixture()
    facts = SQLiteCompanyFactsIndex(tmp_path / "formal.sqlite3", event_policy=event_policy, sqlite_policy=policy)
    reader = SQLiteCompanyEvidenceReader(facts.path, event_policy=event_policy, sqlite_policy=policy)
    documents = CompanyDocumentFiles(tmp_path / "raw/text", max_text_characters=100000, max_bytes=1000000, reserve_bytes=1)
    sources_path = tmp_path / "company_sources.sqlite3"
    material = SQLiteCompanyMaterialIndex(sources_path, facts=facts, documents=documents,
        event_policy=event_policy, sqlite_policy=policy)
    normalized = SQLiteCompanyNormalizedIndex(sources_path, facts, event_policy=event_policy,
        sqlite_policy=policy, clean_material=lambda: None)
    journal = SQLiteCompanySyncRoundJournal(tmp_path / "journal.sqlite3", sqlite_policy=policy, publication_identity=reader.publication_identity)
    inputs = SQLiteCompanySyncInputStore(sources_path, sqlite_policy=policy)
    physical = []
    def execute(request):
        physical.append(request.url)
        return HttpResponse(200, b"ok")
    transport = RequestsHttpTransport(
        monotonic=lambda: 0, authorize=journal.authorize_request,
        observe=journal.observe_attempt, execute_request=execute,
    )
    source = _Sources(transport)
    runtime = CompanySyncRuntime(normalized, facts, material, journal, inputs, event_policy=event_policy, sqlite_policy=policy,
        read_document=documents.read, financial_source=source, ratio_source=source, annual_source=source,
        official_source=source, observe_scope=observe_scope,
        financial_revision_proofs=financial_revision_proofs)
    return runtime, reader, transport, physical


def test_local_field_watermark_reacts_to_proof_inputs_and_coverage_only(tmp_path):
    runtime, _, transport, physical = _runtime(tmp_path)
    with transport:
        runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
        before = runtime.field_watermark(_SCOPE, _SCOPE.codes, lambda: None)
        today = _SCOPE.cutoff.date()
        runtime.inputs.accept_ratios(_SCOPE.identity, StructuredCompanyQuery("unlock", today, today),
            StructuredCompanyPage("unlock", 1, 1, 0), _SCOPE.cutoff)
        runtime.inputs.complete_task(_SCOPE.identity, "news", _SCOPE.codes[0], 1, CanonicalDigest("news_metadata"))
        assert runtime.field_watermark(_SCOPE, _SCOPE.codes, lambda: None) == before

        partition = DisclosurePartition("sse", today, today)
        runtime.material.register_partition(partition, _SCOPE.cutoff)
        runtime.material.commit_page(partition, DisclosurePage((), 0, False), _SCOPE.cutoff)
        covered = runtime.field_watermark(_SCOPE, _SCOPE.codes, lambda: None)
        assert covered != before

        proof = CompanyRecoveredInput(_SCOPE.codes[0], "cninfo", "fixture", "a" * 64, b"b" * 32)
        runtime.inputs.record_recovered(_SCOPE.identity, proof)
        recovered = runtime.field_watermark(_SCOPE, _SCOPE.codes, lambda: None)
        assert recovered != covered
        runtime.inputs.acknowledge_recovered(_SCOPE.identity, (proof,))
        assert runtime.field_watermark(_SCOPE, _SCOPE.codes, lambda: None) == recovered
    assert physical == []


def test_clean_download_reuses_the_ledger_but_not_the_old_active_cursor(tmp_path):
    runtime, reader, transport, physical = _runtime(tmp_path)
    runtime.journal.initialize()
    plans = tuple(CompanySyncPhasePlan(family, CanonicalDigest(family)) for family in _SCOPE.families)
    previous = runtime.journal.begin(_SCOPE, plans, max_source_calls=10)
    for plan in plans:
        announcement = plan.family is CompanySyncFamily.ANNOUNCEMENTS
        runtime.journal.start(previous, plan.family, CompanySyncPhaseState(CanonicalDigest(plan), announcement))
        if announcement:
            runtime.journal.authorize_request(HttpRequest("POST", CNINFO_DIRECTORY_URL, 1, body=b"previous-page=1"))
        runtime.journal.finish(previous, plan.family, CompanySyncStepResult(not announcement, False,
            failure="source_attempt_unconfirmed" if announcement else None))

    with transport:
        with pytest.raises(CompanySyncRebuildActiveRound):
            runtime.initialize(_SCOPE.codes, _SCOPE.cutoff + timedelta(hours=1))
        assert not runtime.facts.path.exists() and not runtime.material.path.exists()
        assert runtime.journal.active().round_id == previous
        tomorrow = _SCOPE.cutoff + timedelta(days=1)
        runtime.initialize(_SCOPE.codes, tomorrow)
        assert runtime.journal.active() is None
        assert reader.read(_SCOPE.codes)[0].financial is None
        result = runtime.run(_SCOPE.codes, tomorrow, CompanySyncBudget(100, 0, 20, 10),
            cancel_requested=lambda: False, monotonic=lambda: 0)
    assert result.scope.cutoff == tomorrow and result.round_id != previous
    assert runtime.journal.source_calls(previous) == 1 and runtime.journal.source_calls(result.round_id) == 0
    assert runtime.journal.family(previous, CompanySyncFamily.ANNOUNCEMENTS).unknown_attempts == 1
    assert physical == []


def test_candidate_watermark_includes_exact_financial_original_and_authority_digest(tmp_path):
    report_date = _SCOPE.cutoff.date() - timedelta(days=30)
    original = Disclosure("sse", "issuer-report", _SCOPE.codes, "季度报告", _SCOPE.cutoff, "", "a" * 64)
    proof = CompanyFinancialRevisionProof(original, report_date, report_date, _SCOPE.cutoff, "b" * 64, b"c" * 32)
    runtime, _, transport, physical = _runtime(tmp_path, financial_revision_proofs=(proof,))
    with transport:
        runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
        partition = DisclosurePartition("sse", _SCOPE.cutoff.date(), _SCOPE.cutoff.date())
        runtime.material.register_partition(partition, _SCOPE.cutoff)
        runtime.material.commit_page(partition, DisclosurePage((), 0, False), _SCOPE.cutoff)
        before = runtime.field_watermark(_SCOPE, _SCOPE.codes, lambda: None)
        runtime.material.register_disclosures((original,))
        after = runtime.field_watermark(_SCOPE, _SCOPE.codes, lambda: None)
        assert after != before
        assert runtime.field_watermark(_SCOPE, _SCOPE.codes, lambda: None) == after
    assert physical == []


def test_broad_directory_duplicates_refine_durable_dates_without_accepting_duplicate_rows(tmp_path):
    runtime, reader, transport, _physical = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    before = reader.read(_SCOPE.codes)

    class Source:
        def fetch(self, partition, *, order=""):
            if partition.start == partition.end == _SCOPE.cutoff.date():
                return DisclosurePage((), 0, False)
            item = Disclosure("cninfo", "duplicate", _SCOPE.codes, "普通公告", _SCOPE.cutoff - timedelta(days=1), "", "a" * 64)
            return DisclosurePage((item, item), 2, False)

    producer = CompanyAnnouncementProducer(runtime.inputs, runtime.material, Source(), _SCOPE,
        sqlite_policy=SqlitePolicyFixture(), extraction_hash=runtime.facts.extraction_hash)
    for _ in range(3):
        result = producer.advance(_SCOPE, cancel_requested=lambda: False, deadline=10)
    assert result.can_continue and result.accepted_inputs == 0 and result.source_calls == 1
    whole = producer._partitions[1]
    children = runtime.material.pending_partitions(whole)
    assert len(children) == 2 and sum((item.end - item.start).days + 1 for item in children) == 365
    assert all(item.page == 1 and item.start <= item.end for item in children)
    assert all((item.start, item.end) != (whole.start, whole.end) for item in children)
    assert reader.read(_SCOPE.codes) == before
    with SqliteSession(runtime.material.path) as connection:
        assert connection.run("SELECT COUNT(*) FROM disclosures").require_one()[0] == 0
    transport.close()


def test_single_day_directory_uses_existing_query_splits_and_checks_parent_union(tmp_path):
    runtime, reader, transport, _physical = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)

    class Source:
        parent_calls = 0

        def fetch(self, partition, *, order=""):
            item = Disclosure("cninfo", "first", _SCOPE.codes, "普通公告", _SCOPE.cutoff, "", "a" * 64)
            if partition.query is None:
                self.parent_calls += 1
                return DisclosurePage((item, item), 2, False) if self.parent_calls == 1 else DisclosurePage((item,), 1, False)
            return DisclosurePage((item,), 1, False) if partition.query.plate == "shmb" else DisclosurePage((), 0, False)

        def split_queries(self, partition):
            return (DisclosureQuery("shmb"), DisclosureQuery("shkcp"))

    source = Source()
    producer = CompanyAnnouncementProducer(runtime.inputs, runtime.material, source, _SCOPE,
        sqlite_policy=SqlitePolicyFixture(), extraction_hash=runtime.facts.extraction_hash)
    for _ in range(5):
        result = producer.advance(_SCOPE, cancel_requested=lambda: False, deadline=10)
    today = producer._partitions[0]
    assert runtime.material.pending_partitions(today) == () and source.parent_calls == 2
    assert result.accepted_inputs == 0 and result.source_calls == 1
    assert reader.read(_SCOPE.codes)[0].risk_events == ()
    transport.close()


def test_unsplittable_single_day_duplicates_remain_rejected(tmp_path):
    runtime, _reader, transport, _physical = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)

    class Source:
        def fetch(self, partition, *, order=""):
            item = Disclosure("cninfo", "duplicate", _SCOPE.codes, "普通公告", _SCOPE.cutoff, "", "a" * 64)
            return DisclosurePage((item, item), 2, False)

        def split_queries(self, partition):
            raise CompanyInputValidationFailure("cninfo_query_unsplittable")

    producer = CompanyAnnouncementProducer(runtime.inputs, runtime.material, Source(), _SCOPE,
        sqlite_policy=SqlitePolicyFixture(), extraction_hash=runtime.facts.extraction_hash)
    producer.advance(_SCOPE, cancel_requested=lambda: False, deadline=10)
    with pytest.raises(CompanyInputValidationFailure, match="cninfo_query_unsplittable"):
        producer.advance(_SCOPE, cancel_requested=lambda: False, deadline=10)
    transport.close()


def test_real_empty_target_initializes_unavailable_and_runs_finite_families_with_explicit_source_gaps(tmp_path):
    runtime, reader, transport, physical = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    initial, = reader.read(_SCOPE.codes)
    assert initial.financial is None and initial.financial_state.value == "unavailable"
    with transport:
        result = runtime.run(_SCOPE.codes, _SCOPE.cutoff, CompanySyncBudget(100, 20, 20, 10),
            cancel_requested=lambda: False, monotonic=lambda: 0)
    assert result.processing_complete and not result.business_complete and not result.cancelled
    assert tuple(item.family for item in result.families) == _SCOPE.families
    assert len(physical) == sum(item.source_calls for item in result.families)
    assert result.publication_hash == reader.publication_identity()
    assert reader.read(_SCOPE.codes) == (initial,)
    assert runtime.journal.can_refresh(result.round_id)


def test_new_same_day_cutoff_cannot_refill_an_exhausted_source_budget(tmp_path):
    runtime, reader, transport, physical = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    with transport:
        first = runtime.run(_SCOPE.codes, _SCOPE.cutoff, CompanySyncBudget(100, 1, 20, 10),
            cancel_requested=lambda: False, monotonic=lambda: 0)
        before = tuple(physical)
        publication = reader.publication_identity()
        second = runtime.run(_SCOPE.codes, _SCOPE.cutoff + timedelta(hours=1), CompanySyncBudget(100, 200, 20, 10),
            cancel_requested=lambda: False, monotonic=lambda: 0)
        with pytest.raises(ValueError, match="company_sync_refresh_cutoff_before_active"):
            runtime.run(_SCOPE.codes, _SCOPE.cutoff - timedelta(minutes=1), CompanySyncBudget(100, 200, 20, 10),
                cancel_requested=lambda: False, monotonic=lambda: 0)
    assert first.budget_exhausted and first.processing_complete
    assert second.round_id == first.round_id and second.scope == first.scope
    assert tuple(physical) == before and runtime.journal.source_calls(first.round_id) == 1
    assert runtime.journal.active().max_source_calls == 1 and reader.publication_identity() == publication


def test_next_day_refresh_keeps_the_old_exhausted_ledger_and_starts_a_dated_round(tmp_path):
    snapshots = []
    runtime, _reader, transport, physical = _runtime(tmp_path, observe_scope=lambda scope: snapshots.append((scope, len(physical))))
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    with transport:
        first = runtime.run(_SCOPE.codes, _SCOPE.cutoff, CompanySyncBudget(100, 1, 20, 10),
            cancel_requested=lambda: False, monotonic=lambda: 0)
        second = runtime.run(_SCOPE.codes, _SCOPE.cutoff + timedelta(days=1), CompanySyncBudget(100, 1, 20, 10),
            cancel_requested=lambda: False, monotonic=lambda: 0)
    assert first.round_id != second.round_id and second.scope.cutoff.date() > first.scope.cutoff.date()
    assert len(physical) == 2 and runtime.journal.source_calls(first.round_id) == runtime.journal.source_calls(second.round_id) == 1
    assert snapshots == [(first.scope, 0), (second.scope, 1)]


def test_new_runtime_scope_excludes_news_and_resumes_same_request_budget(tmp_path):
    runtime, _, transport, physical = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    with transport:
        first = runtime.run(_SCOPE.codes, _SCOPE.cutoff, CompanySyncBudget(1, 20, 20, 10),
            cancel_requested=lambda: False, monotonic=lambda: 0)
        resumed = runtime.run(_SCOPE.codes, _SCOPE.cutoff, CompanySyncBudget(100, 200, 20, 10),
            cancel_requested=lambda: False, monotonic=lambda: 0)
    assert resumed.round_id == first.round_id and resumed.processing_complete
    assert runtime.journal.active().max_source_calls == 20
    assert all(family.value != "news" for family in resumed.scope.families)
    assert "https://example.test/news" not in physical
    with SqliteSession(runtime.inputs.path) as connection:
        assert connection.run("SELECT COUNT(*) FROM company_source_task WHERE family IN ('news','news_scope')").require_one()[0] == 0


@pytest.mark.parametrize("active", (True, False))
def test_unsupported_plan_admission_preserves_charged_requests(tmp_path, active):
    runtime, _, transport, physical = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    plans = tuple(CompanySyncPhasePlan(family, CanonicalDigest(family)) for family in _SCOPE.families)
    round_id = runtime.journal.begin(_SCOPE, plans, max_source_calls=20)
    request_hash = CanonicalDigest("recorded_request")
    with SqliteSession(runtime.journal.path) as connection:
        connection.run("UPDATE company_sync_plan SET position=6 WHERE round_id=? AND family='fields'", (round_id,))
        connection.run("INSERT INTO company_sync_plan VALUES(?,'news',5,?,'[]',0)",
            (round_id, CanonicalDigest("retired_news")))
        connection.run("""INSERT INTO company_sync_step(round_id,family,step,state_hash,source_required)
            VALUES(?,'news',1,?,1)""", (round_id, CanonicalDigest("planned_request")))
        connection.run("""INSERT INTO company_sync_request(round_id,family,step,request_hash,host,method)
            VALUES(?,'news',1,?,'example.test','GET')""", (round_id, request_hash))
        if not active:
            connection.run("UPDATE company_sync_round SET active=0 WHERE round_id=?", (round_id,))
    assert runtime.journal.source_calls(round_id) == 1

    next_cutoff = _SCOPE.cutoff + timedelta(days=1)
    with transport:
        if active:
            with pytest.raises(CompanyDatabaseIncompatibleError) as initialize:
                runtime.initialize(_SCOPE.codes, next_cutoff)
            with pytest.raises(CompanyDatabaseIncompatibleError) as run:
                runtime.run(_SCOPE.codes, next_cutoff, CompanySyncBudget(1, 20, 20, 10),
                    cancel_requested=lambda: False, monotonic=lambda: 0)
            assert initialize.value.store is run.value.store is CompanyDownloadStore.REQUEST_JOURNAL
        else:
            runtime.initialize(_SCOPE.codes, next_cutoff)
            result = runtime.run(_SCOPE.codes, next_cutoff, CompanySyncBudget(1, 20, 20, 10),
                cancel_requested=lambda: False, monotonic=lambda: 0)
            assert result.round_id != round_id and result.scope.cutoff == next_cutoff
    with SqliteSession(runtime.journal.path) as connection:
        original = connection.run("SELECT active,max_source_calls FROM company_sync_round WHERE round_id=?", (round_id,))
        assert tuple(original.require_one()) == (int(active), 20)
        assert tuple(connection.run("SELECT family,request_hash,observed FROM company_sync_request").require_one()) == (
            "news", request_hash, 0,
        )
    assert not physical


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


def test_normal_financial_download_collects_multiple_issuers_in_one_page_without_pdf_fanout(tmp_path):
    from datetime import date
    from trader.download.domain.company_evidence import CompanyFinancial
    from trader.download.domain.company_source_records import CompanyFinancialSourceObservation, CompanyFinancialSourceRecordHash
    runtime, reader, transport, physical = _runtime(tmp_path)
    codes = ("600001", "600002")
    runtime.initialize(codes, _SCOPE.cutoff)
    notice_at = _SCOPE.cutoff - timedelta(days=1)
    observations = []
    for code in codes:
        financial = CompanyFinancial(code, date(2026, 9, 30), notice_at, 1, 2, 3, 4, 5, 6, 7, 8)
        security = f"{code}.SH"
        observations.append(CompanyFinancialSourceObservation(financial, security, notice_at, notice_at, _SCOPE.cutoff,
            "CNY", CompanyFinancialSourceRecordHash(financial, security, notice_at, notice_at, "CNY"), ()))
    def source(query, *, observed_at):
        runtime._financial_source.transport.get("https://example.test/financial", timeout=1)
        return CompanyFinancialSourcePage(1, 1, len(observations), len(observations), tuple(observations), "a" * 64)
    runtime._financial_source.financial_observations = source
    before = reader.read(codes)
    with transport:
        result = runtime.run(codes, _SCOPE.cutoff, CompanySyncBudget(100, 20, 20, 10), cancel_requested=lambda: False, monotonic=lambda: 0)
    assert result.processing_complete and not result.business_complete and reader.read(codes) == before
    assert physical.count("https://example.test/financial") == 1
    with SqliteSession(runtime.normalized.path) as normalized, SqliteSession(runtime.facts.path) as formal:
        assert ReadCompanyFinancialObservations(normalized, codes) == tuple(observations)
        assert ReadCompanyFinancialObservations(formal, codes) == ()
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


def test_actual_local_rule_change_reproves_without_resetting_source_round_or_allowance(tmp_path, monkeypatch):
    from trader.download.infra import company_field_publication
    runtime, reader, transport, physical = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    with transport:
        first = runtime.run(_SCOPE.codes, _SCOPE.cutoff, CompanySyncBudget(100, 20, 20, 10),
            cancel_requested=lambda: False, monotonic=lambda: 0)
    consumed = sum(item.source_calls for item in first.families)
    old_hash = company_field_publication.CompanyFieldProofRulesHash()
    monkeypatch.setattr(company_field_publication, "COMPANY_FIELD_RULES", tuple(
        pair for pair in company_field_publication.COMPANY_FIELD_RULES if pair[0] != "end_date"
    ))
    assert company_field_publication.CompanyFieldProofRulesHash() != old_hash
    runtime, reader, transport, physical = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    with transport:
        final = runtime.run(_SCOPE.codes, _SCOPE.cutoff, CompanySyncBudget(100, 200, 20, 10),
            cancel_requested=lambda: False, monotonic=lambda: 0)
    assert final.round_id == first.round_id and final.scope == first.scope and not physical
    assert runtime.journal.active().max_source_calls == 20
    assert sum(item.source_calls for item in final.families) == consumed
    with SqliteSession(runtime.facts.path) as formal:
        assert formal.run("SELECT rules_hash FROM company_field_proof_rule").require_one()[0] == company_field_publication.CompanyFieldProofRulesHash()


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
