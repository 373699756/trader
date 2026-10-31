"""Bounded company batches preserve checkpoints, document identity and publications."""

from tests.helpers.configuration import SqlitePolicyFixture

from dataclasses import replace
from datetime import timedelta
from threading import Barrier, Event, get_ident

import pytest

from tests.component import test_company_evidence as fixtures
from trader.download.application.company_evidence import CompanyEvidenceDependencies
from trader.download.domain.company_evidence import DisclosurePage, DisclosurePartition
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction
from trader.infra.workers import BoundedExecutor
from tests.helpers.configuration import (
    CompanyEventPolicyFixture as CompanyEventPolicy,
    CompanyEvidenceRequestFixture as CompanyEvidenceRequest,
    CompanyDocumentFilesFixture as CompanyDocumentFiles,
    SQLiteCompanyEvidenceIndexFixture as SQLiteCompanyEvidenceIndex,
    SQLiteCompanyEvidenceReaderFixture as SQLiteCompanyEvidenceReader,
)

NOW = fixtures.NOW


def test_ocr_timeout_retries_once_and_transport_failure_preserves_budget(tmp_path):
    import json
    from types import SimpleNamespace

    from trader.download.application.company_evidence import _DocumentRun, _SourceCircuit, _SyncState
    from trader.download.domain.company_evidence import CompanyDocumentFailure, CompanyOcrDiagnostic

    index = fixtures._index(tmp_path)
    item = _seed(index, 1)[0]
    policies = fixtures._TEST_DOCUMENT_POLICIES
    run = _DocumentRun.__new__(_DocumentRun)
    run.dependencies = SimpleNamespace(index=index, monotonic=lambda: 0.0)
    run.failed_identities = set()
    run.state = _SyncState()
    run.policies = policies
    run.configuration = fixtures._TEST_COMPANY_CONFIGURATION
    run.observed_at, run.started = NOW, 0.0
    run.circuit = _SourceCircuit()
    diagnostic = CompanyOcrDiagnostic("recognition", 2, 3, 91, 24.0, 3)
    failure = CompanyDocumentFailure("disclosure_document_ocr_timeout", payload_hash=b"a" * 32, diagnostic=diagnostic)
    run.failed(item, failure)
    assert index.document_ocr_timeouts(item, policies.ocr) == 1
    assert index.document_workload(("600001",), NOW, policies).cooling == 1
    assert index.document_workload(("600001",), NOW + timedelta(minutes=16), policies).due == 1
    run.failed(item, TimeoutError("download"))
    assert index.document_ocr_timeouts(item, policies.ocr) == 1
    run.failed(item, failure)
    assert index.document_ocr_timeouts(item, policies.ocr) == 2
    workload = index.document_workload(("600001",), NOW + timedelta(days=1), policies)
    assert workload.due == workload.cooling == 0 and workload.unextractable == 1
    with SqliteSession(index.path) as connection:
        row = connection.run("SELECT failure,ocr_diagnostic FROM document_retries").require_one()
        assert row[0] == "disclosure_document_ocr_timeout_exhausted"
        assert json.loads(row[1])["phase"] == "recognition"
    from scripts.runtime_diagnostics.company_query_audit import AuditCompanyStorage

    audit = AuditCompanyStorage(index.path, now=NOW, budget_seconds=5, sqlite_policy=SqlitePolicyFixture())
    assert audit["summary"]["local_storage"]["ocr_diagnostics"][0]["diagnostic"]["phase"] == "recognition"
    changed = replace(policies, ocr="next-ocr-policy")
    assert index.document_workload(("600001",), NOW, changed).due == 1
    assert index.document_ocr_timeouts(item, changed.ocr) == 0


@pytest.mark.parametrize("route,failure", [("ocr", "disclosure_document_no_text"), ("limits", "disclosure_document_page_limit")])
def test_targeted_document_repair_publishes_risk_and_releases_body(tmp_path, route, failure):
    from trader.download.application.company_evidence import SyncCompanyEvidence

    index = fixtures._index(tmp_path)
    item = _seed(index, 1)[0]
    index.document_failure(item, "old-policy", failure, NOW, NOW + timedelta(days=1))
    index.publish(("600001",), NOW)
    reader = SQLiteCompanyEvidenceReader(index.facts_path, raw_path=index.path)
    assert not reader.read(("600001",))[0].risk_events

    class Source(fixtures.Sources):
        def document(self, disclosure, *, route="standard"):
            return fixtures._extraction("本公司收到中国证监会立案告知书。")

    source = Source()
    request = CompanyEvidenceRequest(("600001",), NOW, fixtures._TEST_COMPANY_CONFIGURATION, document_repair=route)
    result = SyncCompanyEvidence(CompanyEvidenceDependencies(index, source, source, lambda: 0.0), request)
    assert not result.needs_retry and result.changed_rows == 1
    assert len(reader.read(("600001",))[0].risk_events) == 1
    assert not source.calls
    assert not tuple((tmp_path / "text").glob("*/*.txt.gz"))


def test_targeted_repair_cancellation_preserves_old_publication(tmp_path):
    from trader.download.application.company_evidence import SyncCompanyEvidence

    index = fixtures._index(tmp_path)
    item = _seed(index, 1)[0]
    index.document_failure(item, "old-policy", "disclosure_document_no_text", NOW, NOW + timedelta(days=1))
    index.publish(("600001",), NOW)
    reader = SQLiteCompanyEvidenceReader(index.facts_path, raw_path=index.path)
    previous = reader.read(("600001",))
    cancelled = Event()

    class Source(fixtures.Sources):
        def document(self, disclosure, *, route="standard"):
            cancelled.set()
            return fixtures._extraction("本公司收到中国证监会立案告知书。")

    source = Source()
    request = CompanyEvidenceRequest(("600001",), NOW, fixtures._TEST_COMPANY_CONFIGURATION, document_repair="ocr")
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0.0, cancel_requested=cancelled.is_set)
    assert SyncCompanyEvidence(dependencies, request).needs_retry
    assert reader.read(("600001",)) == previous
    cancelled.clear()
    # The accepted body must publish on resume even though no download remains.
    SyncCompanyEvidence(dependencies, request)
    assert len(reader.read(("600001",))[0].risk_events) == 1


def _seed(index, count=4):
    partition = next(p for p in index.partitions(("sse",), NOW) if p.start == NOW.date())
    items = tuple(fixtures._disclosure(str(i), "关于收到立案告知书的公告") for i in range(count))
    index.commit_page(partition, DisclosurePage(items, count, False), NOW)
    return items


def test_page_batch_publishes_before_directory_finishes_and_restart_keeps_cursor(tmp_path):
    cancelled = Event()
    events = []
    calls = []

    class Index(SQLiteCompanyEvidenceIndex):
        def partitions(self, markets, observed_at):
            return tuple(p for p in super().partitions(markets, observed_at) if p.start == NOW.date())

    class Source(fixtures.Sources):
        def fetch(self, query, **kwargs):
            if not isinstance(query, DisclosurePartition):
                return super().fetch(query, **kwargs)
            calls.append(query.page)
            item = fixtures._disclosure(str(query.page), "关于收到立案告知书的公告")
            return DisclosurePage((item,), 3, query.page < 3)

        def document(self, item):
            return fixtures._extraction("本公司收到中国证监会立案告知书。")

    def progress(event):
        events.append(event)
        if event.component == "publish" and event.status == "complete":
            cancelled.set()

    source = Source()
    index = Index(
        tmp_path / "facts.sqlite3",
        facts_path=tmp_path / "company_facts.sqlite3",
        documents=CompanyDocumentFiles(tmp_path / "text"),
        cancel_requested=cancelled.is_set,
    )
    config = replace(fixtures._TEST_COMPANY_CONFIGURATION, directory_batch_pages=1)
    first = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0, progress, cancelled.is_set),
        ("600001",),
        NOW,
        configuration=config,
    )
    assert "task:cancelled" in first.failures and calls == [1]
    assert SQLiteCompanyEvidenceReader(index.facts_path, raw_path=index.path).read(("600001",))[0].risk_events
    cancelled.clear()
    second = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0),
        ("600001",),
        NOW,
        configuration=config,
    )
    assert calls == [1, 2, 3] and second.pending_documents == 0
    assert second.published_codes == 1


def test_two_clients_overlap_and_only_main_thread_writes_accepted_results_on_cancel(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    items = _seed(index)
    monkeypatch.setattr(index, "partitions", lambda *_: ())
    cancelled = Event()
    barrier = Barrier(2)
    worker_threads = []
    write_threads = []
    commit = index.commit_document

    def observed_commit(item, extraction, extracted_at):
        write_threads.append(get_ident())
        return commit(item, extraction, extracted_at)

    monkeypatch.setattr(index, "commit_document", observed_commit)

    class Source(fixtures.Sources):
        def document(self, item):
            worker_threads.append(get_ident())
            barrier.wait(timeout=3)
            cancelled.set()
            return fixtures._extraction("本公司收到中国证监会立案告知书。")

    clients = (Source(), Source())
    workers = BoundedExecutor(worker_count=2, queue_capacity=2, thread_name_prefix="test-company")
    workers.start()
    try:
        result = fixtures._sync_company_evidence(
            CompanyEvidenceDependencies(
                index,
                clients[0],
                clients[0],
                lambda: 0,
                cancel_requested=cancelled.is_set,
                document_sources=clients,
                document_executor=workers,
            ),
            ("600001",),
            NOW,
            configuration=replace(fixtures._TEST_COMPANY_CONFIGURATION, document_workers=2),
        )
    finally:
        workers.stop(wait=True, cancel_futures=True)
    assert "task:cancelled" in result.failures
    assert len(set(worker_threads)) == 2 and write_threads == [get_ident(), get_ident()]
    pending = index.pending_documents(("600001",), NOW, policies=fixtures._TEST_DOCUMENT_POLICIES)
    assert len(pending) == len(items) - 2
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT COUNT(*) FROM document_retries").require_one()[0] == 0


def test_revision_rejects_late_body_and_dirty_publication_retains_other_issuers(tmp_path):
    index = fixtures._index(tmp_path)
    item = _seed(index, 1)[0]
    index.publish(("600001", "600002"), NOW)
    reader = SQLiteCompanyEvidenceReader(index.facts_path, raw_path=index.path)
    previous_other = reader.read(("600002",))[0]
    changed = replace(item, digest="revised")
    partition = DisclosurePartition("sse", NOW.date(), NOW.date())
    index.commit_page(partition, DisclosurePage((changed,), 1, False), NOW)
    assert not index.commit_document(item, fixtures._extraction("过期正文"), NOW)
    assert tuple(task.disclosure for task in index.pending_documents(("600001",), NOW, policies=fixtures._TEST_DOCUMENT_POLICIES)) == (changed,)
    assert index.commit_document(changed, fixtures._extraction("本公司收到中国证监会立案告知书。"), NOW)
    assert index.publish(("600001",), NOW, changed_only=True) == 1
    assert reader.read(("600002",))[0] == previous_other
    assert index.publish(("600001",), NOW, changed_only=True) == 0
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT COUNT(*) FROM company_dirty_codes WHERE code='600001'").require_one()[0] == 0


def test_document_revision_removes_stale_success_metadata(tmp_path):
    index = fixtures._index(tmp_path)
    item = _seed(index, 1)[0]
    assert index.commit_document(item, fixtures._extraction("原公告完整正文"), NOW)
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT COUNT(*) FROM document_extractions").require_one()[0] == 1
    revised = replace(item, digest="revised")
    index.commit_page(
        DisclosurePartition("sse", NOW.date(), NOW.date()), DisclosurePage((revised,), 1, False), NOW
    )
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT COUNT(*) FROM document_extractions").require_one()[0] == 0


def test_rule_change_rebuilds_all_existing_issuers_when_requested_subset(tmp_path):
    index = fixtures._index(tmp_path)
    index.publish(("600001", "600002"), NOW)
    policy = replace(CompanyEventPolicy(), risk_days=366)
    changed = SQLiteCompanyEvidenceIndex(
        index.path,
        facts_path=index.facts_path,
        documents=CompanyDocumentFiles(index.path.parent / "text"),
        event_policy=policy,
    )
    changed.initialize()
    assert changed.publish(("600001",), NOW, changed_only=True) == 2
    assert (
        len(
            SQLiteCompanyEvidenceReader(index.facts_path, raw_path=index.path, event_policy=policy).read(
                ("600001", "600002")
            )
        )
        == 2
    )


def test_batch_crossing_midnight_rechecks_clean_requested_stocks(tmp_path):
    index = fixtures._index(tmp_path)
    codes = ("600001", "600002")
    index.publish(codes, NOW)
    assert index.publish(codes, NOW, changed_only=True) == 0
    assert index.publish(codes, NOW + timedelta(days=1), changed_only=True) == 2


def test_legacy_cursor_backfill_and_unverified_page_size_never_reset_saved_page(tmp_path):
    index = fixtures._index(tmp_path)
    partition = index.partitions(("sse",), NOW)[0]
    item = fixtures._disclosure()
    index.commit_page(partition, DisclosurePage((item,), 2, True), NOW)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        for table in (
            "company_dirty_codes",
            "company_document_requirements",
            "company_work_policy",
            "disclosure_query_contracts",
        ):
            connection.run(f"DROP TABLE {table}")
    index.initialize()
    resumed = next(p for p in index.partitions(("sse",), NOW) if p.start == partition.start)
    assert resumed.page == 2
    with pytest.raises(ValueError, match="query_contract_unverified"):
        index.commit_page(replace(resumed, page_size=100), DisclosurePage((), 2, False), NOW)
    assert next(p for p in index.partitions(("sse",), NOW) if p.start == partition.start).page == 2


def test_shared_document_is_selected_once_and_cooling_does_not_starve_ready_documents(tmp_path):
    index = fixtures._index(tmp_path)
    items = _seed(index)
    shared = replace(items[0], codes=("600001", "600002"))
    partition = DisclosurePartition("sse", NOW.date(), NOW.date())
    index.commit_page(partition, DisclosurePage((shared, *items[1:]), len(items), False), NOW)
    index.document_failure(
        shared, fixtures._TEST_DOCUMENT_POLICIES.standard, "timeout", NOW, NOW + timedelta(minutes=15)
    )
    pending = index.pending_documents(
        ("600001", "600002"), NOW, limit=2, policies=fixtures._TEST_DOCUMENT_POLICIES
    )
    assert len(pending) == 2 and shared.identity not in {task.disclosure.identity for task in pending}
    workload = index.document_workload(("600001", "600002"), NOW, fixtures._TEST_DOCUMENT_POLICIES)
    assert (workload.pending, workload.due, workload.cooling) == (4, 3, 1)


def test_transient_failure_attempted_in_this_run_stays_cooling_after_retry_time(tmp_path):
    index = fixtures._index(tmp_path)
    item = _seed(index, 1)[0]
    index.document_failure(
        item,
        fixtures._TEST_DOCUMENT_POLICIES.standard,
        "disclosure_document_parse_timeout",
        NOW,
        NOW + timedelta(seconds=1),
    )
    workload = index.document_workload(
        ("600001",),
        NOW + timedelta(hours=1),
        fixtures._TEST_DOCUMENT_POLICIES,
        excluded=((item.source, item.identity, item.digest),),
    )
    assert (workload.pending, workload.due, workload.cooling) == (1, 0, 1)


def test_poppler_policy_upgrade_reopens_old_parser_timeout_without_waiting(tmp_path):
    index = fixtures._index(tmp_path)
    item = _seed(index, 1)[0]
    index.document_failure(
        item,
        "text_3:10:16777216:200:1000000",
        "disclosure_document_parse_timeout",
        NOW,
        NOW + timedelta(days=1),
    )
    policies = replace(fixtures._TEST_DOCUMENT_POLICIES, standard="text_4:poppler-fixture:10:16777216:200:1000000")
    workload = index.document_workload(("600001",), NOW, policies)
    assert (workload.pending, workload.due, workload.cooling) == (1, 1, 0)


def test_deterministic_document_failure_reopens_only_for_matching_route_policy_or_content(tmp_path):
    index = fixtures._index(tmp_path)
    item = _seed(index, 1)[0]
    old_policy = "old-text-policy"
    index.document_failure(item, old_policy, "disclosure_document_no_text", NOW, NOW + timedelta(days=1))

    policies = fixtures._TEST_DOCUMENT_POLICIES
    tasks = index.pending_documents(("600001",), NOW, limit=10, policies=policies, repair="ocr")
    assert [(task.disclosure.identity, task.route) for task in tasks] == [(item.identity, "ocr")]

    index.document_failure(
        item,
        policies.ocr,
        "disclosure_document_ocr_low_quality",
        NOW,
        NOW + timedelta(seconds=1),
    )
    workload = index.document_workload(("600001",), NOW + timedelta(days=2), policies, repair="ocr")
    assert (workload.pending, workload.due, workload.unextractable) == (1, 0, 1)
    assert index.pending_documents(
        ("600001",), NOW + timedelta(days=2), limit=10, policies=policies, repair="ocr"
    ) == ()

    changed = replace(policies, ocr="test-ocr-v2")
    assert index.document_workload(("600001",), NOW, changed, repair="ocr").due == 1
    revised = replace(item, digest="revised")
    partition = DisclosurePartition("sse", NOW.date(), NOW.date())
    index.commit_page(partition, DisclosurePage((revised,), 1, False), NOW)
    assert index.document_workload(("600001",), NOW, policies).due == 1
