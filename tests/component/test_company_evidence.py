"""Fresh SQLite, production use case and reader contracts without supplier writes."""

import json
import sqlite3
import time
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.reference_fixtures import PreparedFixture
from trader.download.application.company_evidence import CompanyEvidenceDependencies, SyncCompanyEvidence
from trader.download.domain.company_evidence import (
    CompanyRatio,
    CompanySourceFailure,
    Disclosure,
    DisclosurePage,
    DisclosurePartition,
    StructuredCompanyPage,
    StructuredCompanyQuery,
)
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex, SQLiteCompanyEvidenceReader
from trader.infra.http_contracts import HttpResponse, HttpTimeout
from trader.infra.sqlite_connection import (
    SqliteConnectionOptions,
    SqliteSession,
    SqliteTransaction,
)
from trader.recommendation.infra.market_data.published_company_research import PublishedCompanyResearch

NOW = datetime(2026, 10, 18, 9, 15, tzinfo=ZoneInfo("Asia/Shanghai"))


class Sources:
    def __init__(self):
        self.calls = []
        self.failure = False

    def fetch(self, first, *args, **kwargs):
        self.calls.append(first)
        if self.failure:
            raise TimeoutError("fixture")
        if isinstance(first, DisclosurePartition):
            return DisclosurePage((), 0, False)
        return StructuredCompanyPage(first.component, first.page, 1, 0)

    def document(self, item, *, deadline):
        raise AssertionError("ordinary announcements must not fetch a document")

    def pledge_day(self, observed_at, *, deadline):
        return observed_at.date()

    def news(self, code, **kwargs):
        self.calls.append(("news", code))
        return ()


def _index(tmp_path):
    index = SQLiteCompanyEvidenceIndex(tmp_path / "company_evidence.sqlite3")
    index.initialize()
    return index


def _disclosure(identity="first", title="普通公告", digest="a", when=NOW):
    return Disclosure("cninfo", identity, ("600001",), title, when, "", digest)


def test_second_run_reuses_baseline_without_redownloading(tmp_path):
    index = _index(tmp_path)
    source = Sources()
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0.0)
    first = SyncCompanyEvidence(dependencies, ("600001",), NOW)
    assert not first.needs_retry
    assert first.fetched_pages > 3
    source.calls.clear()
    second = SyncCompanyEvidence(dependencies, ("600001",), NOW)
    assert second.fetched_pages == second.changed_rows == 0
    assert not source.calls
    assert second.published_codes == 1
    snapshot = SQLiteCompanyEvidenceReader(index.path).read(("600001",))[0]
    assert snapshot.announcement_complete
    assert snapshot.window_start == NOW.date() - timedelta(days=365)


def test_page_rows_and_cursor_rollback_together(tmp_path):
    index = _index(tmp_path)
    partition = index.partitions(("sse",), NOW)[0]
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run(
            "CREATE TRIGGER refuse_cursor BEFORE UPDATE ON partitions BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
    with pytest.raises(RuntimeError, match="persistence_unavailable"):
        index.commit_page(partition, DisclosurePage((_disclosure(),), 1, False), NOW)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        assert connection.run("SELECT count(*) FROM disclosures").fetchone()[0] == 0
        assert connection.run("SELECT MAX(next_page) FROM partitions").fetchone()[0] == 1


def test_restart_resumes_first_uncommitted_page(tmp_path):
    index = _index(tmp_path)
    partition = index.partitions(("sse",), NOW)[0]
    index.commit_page(partition, DisclosurePage((_disclosure(),), 2, True), NOW)
    resumed = SQLiteCompanyEvidenceIndex(index.path).partitions(("sse",), NOW)
    current = next(item for item in resumed if (item.start, item.end) == (partition.start, partition.end))
    assert (current.page, current.received_rows, current.expected_rows) == (2, 1, 2)
    index.commit_page(current, DisclosurePage((_disclosure("second"),), 2, False), NOW)
    assert all(item != current for item in index.partitions(("sse",), NOW))


def test_unchanged_announcement_does_not_rewrite_document(tmp_path):
    index = _index(tmp_path)
    partition = index.partitions(("sse",), NOW)[0]
    item = _disclosure()
    assert index.commit_page(partition, DisclosurePage((item,), 1, False), NOW) == 1
    index.commit_document(item, "verified body")
    assert index.commit_page(partition, DisclosurePage((item,), 1, False), NOW) == 0
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        assert tuple(connection.run("SELECT body,body_checked FROM disclosures").fetchone()) == ("verified body", 1)
    changed = replace(item, title="更正公告", digest="b")
    assert index.commit_page(partition, DisclosurePage((changed,), 1, False), NOW) == 1
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        assert tuple(connection.run("SELECT body,body_checked FROM disclosures").fetchone()) == ("", 0)


def test_page_batch_rolls_back_partial_links_and_preserves_change_counts(tmp_path):
    index = _index(tmp_path)
    partition = index.partitions(("sse",), NOW)[0]
    rows = (
        replace(_disclosure(), codes=("600001", "600003")),
        replace(_disclosure("second"), codes=("600002",)),
    )
    page = DisclosurePage(rows, 2, False)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run(
            "CREATE TRIGGER refuse_link BEFORE INSERT ON disclosure_codes WHEN NEW.code='600002' "
            "BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
    with pytest.raises(RuntimeError, match="persistence_unavailable"):
        index.commit_page(partition, page, NOW)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        assert connection.run("SELECT COUNT(*) FROM disclosures").require_one()[0] == 0
        assert connection.run("SELECT COUNT(*) FROM disclosure_codes").require_one()[0] == 0
        assert connection.run("SELECT COUNT(*) FROM partition_rows").require_one()[0] == 0
        connection.run("DROP TRIGGER refuse_link")
    assert index.commit_page(partition, page, NOW) == 2
    assert index.commit_page(partition, page, NOW) == 0
    with SqliteSession(index.path) as connection:
        assert [
            tuple(row)
            for row in connection.run(
                "SELECT d.source_id,c.code FROM disclosures d JOIN disclosure_codes c "
                "ON c.disclosure_id=d.id ORDER BY c.code"
            )
        ] == [("first", "600001"), ("second", "600002"), ("first", "600003")]


def test_partial_page_never_removes_existing_links_and_truncation_cannot_complete(tmp_path):
    index = _index(tmp_path)
    partition = index.partitions(("sse",), NOW)[0]
    index.commit_page(partition, DisclosurePage((_disclosure(),), 1, False), NOW)
    with pytest.raises(ValueError, match="truncated"):
        index.commit_page(partition, DisclosurePage((), 1, False), NOW)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        assert connection.run("SELECT count(*) FROM disclosure_codes").fetchone()[0] == 1


def test_publication_isolated_from_unpublished_changes_and_failure(tmp_path):
    index = _index(tmp_path)
    partition = index.partitions(("sse",), NOW)[0]
    index.commit_page(partition, DisclosurePage((_disclosure(),), 1, False), NOW)
    index.publish(("600001",), NOW)
    reader = SQLiteCompanyEvidenceReader(index.path)
    before = reader.read(("600001",))
    index.commit_page(partition, DisclosurePage((_disclosure(title="修订", digest="b"),), 1, False), NOW)
    assert reader.read(("600001",)) == before
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run(
            "CREATE TRIGGER refuse_publication BEFORE UPDATE ON published BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
    with pytest.raises(RuntimeError, match="persistence_unavailable"):
        index.publish(("600001",), NOW)
    assert reader.read(("600001",)) == before


def test_reader_does_not_initialize_or_write_missing_database(tmp_path):
    reader = SQLiteCompanyEvidenceReader(tmp_path / "missing.sqlite3")
    research = PublishedCompanyResearch(
        reader, wall_clock=lambda: NOW, open_dates=lambda: PreparedFixture(NOW).open_dates, monotonic=time.monotonic
    )
    research.initialize(("600001", "600002"), cancelled=lambda: False)
    assert not research.load(("600001",), NOW, include_structured=True)
    assert not reader.path.exists()


def test_reader_releases_transaction_before_consumers_and_pins_all_requested_rows(tmp_path):
    index = _index(tmp_path)
    codes = ("600002", "600001")
    index.publish(codes, NOW)
    reader = SQLiteCompanyEvidenceReader(index.path)
    before = reader.read(codes)
    observed = []

    def consume(snapshot):
        if not observed:
            index.publish(codes, NOW + timedelta(days=1))
            with (
                SqliteSession(index.path, SqliteConnectionOptions(timeout_seconds=0)) as connection,
                SqliteTransaction(connection),
            ):
                assert connection.run("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        observed.append(snapshot)

    reader.visit(codes, consume)
    assert tuple(observed) == before
    assert reader.read(codes) != before


def test_company_observer_requires_restart_to_adopt_manual_publication(tmp_path):
    index = _index(tmp_path)
    source = Sources()
    SyncCompanyEvidence(CompanyEvidenceDependencies(index, source, source, lambda: 0), ("600001",), NOW)
    research = PublishedCompanyResearch(
        SQLiteCompanyEvidenceReader(index.path),
        wall_clock=lambda: NOW,
        open_dates=lambda: PreparedFixture(NOW).open_dates,
        monotonic=time.monotonic,
    )
    research.initialize(("600001", "600002"), cancelled=lambda: False)
    before = research.load(("600001",), NOW, include_structured=True)
    research.observe_publication()
    index.commit_structured(
        StructuredCompanyQuery("pledge", NOW.date(), NOW.date()),
        StructuredCompanyPage("pledge", 1, 1, 1, ratios=(CompanyRatio("600001", NOW.date(), 80),)),
        NOW,
    )
    index.publish(("600001",), NOW)
    research.observe_publication()
    after = research.cached(("600001",), include_structured=True)
    assert after == before
    assert research.status().restart_required
    restarted = PublishedCompanyResearch(
        SQLiteCompanyEvidenceReader(index.path),
        wall_clock=lambda: NOW,
        open_dates=lambda: PreparedFixture(NOW).open_dates,
        monotonic=time.monotonic,
    )
    restarted.initialize(("600001",), cancelled=lambda: False)
    assert restarted.load(("600001",), NOW, include_structured=True)["600001"].pledge_ratio_pct == 80


def test_company_observer_leaves_missing_codes_uncovered_after_later_publication(tmp_path):
    index = _index(tmp_path)
    index.publish(("600001",), NOW)
    research = PublishedCompanyResearch(
        SQLiteCompanyEvidenceReader(index.path),
        wall_clock=lambda: NOW,
        open_dates=lambda: PreparedFixture(NOW).open_dates,
        monotonic=time.monotonic,
    )
    research.initialize(("600001", "600002"), cancelled=lambda: False)
    assert not research.load(("600002",), NOW, include_structured=True)
    research.observe_publication()
    index.publish(("600002",), NOW)
    research.observe_publication()
    assert research.status().entries == 1
    assert research.status().restart_required
    assert not research.cached(("600002",), include_structured=True)


def test_company_observer_recovers_transient_read_error_with_unchanged_version(tmp_path, monkeypatch):
    index = _index(tmp_path)
    index.publish(("600001",), NOW)
    reader = SQLiteCompanyEvidenceReader(index.path)
    research = PublishedCompanyResearch(
        reader, wall_clock=lambda: NOW, open_dates=lambda: PreparedFixture(NOW).open_dates, monotonic=time.monotonic
    )
    research.initialize(("600001", "600002"), cancelled=lambda: False)
    research.load(("600001",), NOW, include_structured=True)
    research.observe_publication()
    original = reader.publication_version

    def unavailable():
        raise RuntimeError("temporarily_unavailable")

    monkeypatch.setattr(reader, "publication_version", unavailable)
    research.observe_publication()
    assert research.status().last_error
    monkeypatch.setattr(reader, "publication_version", original)
    research.observe_publication()
    assert not research.status().last_error


def test_reader_and_candidate_load_never_invoke_background_download(tmp_path):
    index = _index(tmp_path)
    source = Sources()
    SyncCompanyEvidence(CompanyEvidenceDependencies(index, source, source, lambda: 0), ("600001",), NOW)

    def forbidden(*args, **kwargs):
        raise AssertionError("hot path network")

    research = PublishedCompanyResearch(
        SQLiteCompanyEvidenceReader(index.path),
        wall_clock=lambda: NOW,
        open_dates=lambda: PreparedFixture(NOW).open_dates,
        monotonic=time.monotonic,
    )
    research.initialize(("600001", "600002"), cancelled=lambda: False)
    observations = research.load(("600001",), NOW, include_structured=True)
    assert observations["600001"].corporate_risk_window_complete
    assert research.cached(("600001",), include_structured=True) == observations


def test_old_database_is_rejected_without_mutation(tmp_path):
    path = tmp_path / "company_evidence.sqlite3"
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run("CREATE TABLE risk_evidence_recent(payload TEXT)")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="contract_mismatch"):
        SQLiteCompanyEvidenceIndex(path).initialize()
    assert path.read_bytes() == before


def test_corrupt_database_initialization_reports_the_business_error(tmp_path):
    path = tmp_path / "company_evidence.sqlite3"
    path.write_bytes(b"not a SQLite database")
    with pytest.raises(RuntimeError, match="company_evidence_persistence_unavailable") as error:
        SQLiteCompanyEvidenceIndex(path).initialize()
    assert isinstance(error.value.__cause__, sqlite3.DatabaseError)


def test_new_day_queries_only_increment_overlap_and_one_month_audit(tmp_path):
    index = _index(tmp_path)
    source = Sources()
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0)
    SyncCompanyEvidence(dependencies, ("600001",), NOW)
    source.calls.clear()
    SyncCompanyEvidence(dependencies, ("600001",), NOW + timedelta(days=1))
    partitions = [item for item in source.calls if isinstance(item, DisclosurePartition)]
    assert partitions
    assert sum((item.end - item.start).days > 1 for item in partitions) <= 1
    assert max((item.end - item.start).days for item in partitions) <= 31


def test_optional_news_uses_only_candidate_scope(tmp_path):
    index = _index(tmp_path)
    source = Sources()
    SyncCompanyEvidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0), ("600001", "600002"), NOW, news_codes=("600001",)
    )
    assert ("news", "600001") in source.calls
    assert ("news", "600002") not in source.calls


def test_same_day_later_run_checks_new_disclosures_without_restarting_year(tmp_path):
    index = _index(tmp_path)
    source = Sources()
    deps = CompanyEvidenceDependencies(index, source, source, lambda: 0)
    SyncCompanyEvidence(deps, ("600001",), NOW)
    source.calls.clear()
    SyncCompanyEvidence(deps, ("600001",), NOW + timedelta(minutes=30))
    partitions = [item for item in source.calls if isinstance(item, DisclosurePartition)]
    assert len(partitions) == 7
    assert all(item.start == item.end for item in partitions)


def test_duplicate_page_links_restart_without_claiming_complete(tmp_path):
    index = _index(tmp_path)
    part = index.partitions(("sse",), NOW)[0]
    index.commit_page(part, DisclosurePage((_disclosure(),), 2, True), NOW)
    continued = replace(part, page=2, received_rows=1, expected_rows=2)
    with pytest.raises(ValueError, match="duplicate"):
        index.commit_page(continued, DisclosurePage((_disclosure(),), 2, False), NOW)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        assert connection.run("SELECT COUNT(*) FROM coverage").fetchone()[0] == 0
    assert index.partitions(("sse",), NOW)[0].page == 1


def test_unchanged_publication_does_not_rewrite_rows_or_change_version(tmp_path):
    index = _index(tmp_path)
    source = Sources()
    SyncCompanyEvidence(CompanyEvidenceDependencies(index, source, source, lambda: 0), ("600001",), NOW)
    reader = SQLiteCompanyEvidenceReader(index.path)
    before = reader.read(("600001",))
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run(
            "CREATE TRIGGER no_rewrite BEFORE UPDATE ON published BEGIN SELECT RAISE(ABORT,'unexpected rewrite'); END"
        )
    index.publish(("600001",), NOW + timedelta(minutes=1))
    assert reader.read(("600001",)) == before


def test_ratio_snapshot_retraction_and_failed_page_preserve_last_complete_snapshot(tmp_path):
    index = _index(tmp_path)
    start, end = NOW.date(), NOW.date() + timedelta(days=90)
    one = CompanyRatio("600001", start, 10)
    query = StructuredCompanyQuery("unlock", start, end)
    index.commit_structured(query, StructuredCompanyPage("unlock", 1, 1, 1, ratios=(one,)), NOW)
    index.commit_structured(
        query,
        StructuredCompanyPage("unlock", 1, 2, 2, ratios=(replace(one, ratio_pct=20),)),
        NOW,
    )
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        assert connection.run("SELECT ratio_pct FROM ratios").fetchone()[0] == 10
    with pytest.raises(ValueError, match="incomplete"):
        index.commit_structured(
            replace(query, page=2, received_rows=1, expected_rows=2), StructuredCompanyPage("unlock", 2, 2, 2), NOW
        )
    index.commit_structured(query, StructuredCompanyPage("unlock", 1, 1, 0), NOW)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        assert connection.run("SELECT COUNT(*) FROM ratios").fetchone()[0] == 0


def test_corrupt_read_model_is_rejected_and_previous_memory_is_retained(tmp_path):
    index = _index(tmp_path)
    index.publish(("600001",), NOW)
    reader = SQLiteCompanyEvidenceReader(index.path)
    research = PublishedCompanyResearch(
        reader, wall_clock=lambda: NOW, open_dates=lambda: PreparedFixture(NOW).open_dates, monotonic=time.monotonic
    )
    research.initialize(("600001", "600002"), cancelled=lambda: False)
    before = research.load(("600001",), NOW, include_structured=True)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE published SET payload=replace(payload,'window_incomplete','unexpected')")
    with pytest.raises(ValueError, match="digest"):
        reader.read(("600001",))
    assert research.load(("600001",), NOW, include_structured=True) == before
    restarted = PublishedCompanyResearch(
        reader,
        wall_clock=lambda: NOW,
        open_dates=lambda: PreparedFixture(NOW).open_dates,
        monotonic=time.monotonic,
    )
    restarted.initialize(("600001",), cancelled=lambda: False)
    assert not restarted.load(("600001",), NOW, include_structured=True)
    assert "ValueError" in restarted.status().last_error


def test_missing_pledge_record_becomes_zero_only_after_full_snapshot(tmp_path):
    index = _index(tmp_path)
    index.publish(("600001",), NOW)
    reader = SQLiteCompanyEvidenceReader(index.path)
    assert reader.read(("600001",))[0].pledge is None
    index.commit_structured(
        StructuredCompanyQuery("pledge", NOW.date(), NOW.date()), StructuredCompanyPage("pledge", 1, 1, 0), NOW
    )
    index.publish(("600001",), NOW)
    assert reader.read(("600001",))[0].pledge.ratio_pct == 0


def test_cached_snapshot_ages_without_network_or_another_database_read(tmp_path, monkeypatch):
    index = _index(tmp_path)
    source = Sources()
    SyncCompanyEvidence(CompanyEvidenceDependencies(index, source, source, lambda: 0), ("600001",), NOW)
    reader = SQLiteCompanyEvidenceReader(index.path)
    research = PublishedCompanyResearch(
        reader, wall_clock=lambda: NOW, open_dates=lambda: PreparedFixture(NOW).open_dates, monotonic=time.monotonic
    )
    research.initialize(("600001", "600002"), cancelled=lambda: False)
    assert research.load(("600001",), NOW, include_structured=True)["600001"].corporate_risk_window_complete

    def forbidden(*args, **kwargs):
        raise AssertionError("cached research must not repeat database reads")

    monkeypatch.setattr(reader, "visit", forbidden)
    restrictions = {}
    later = research.load(
        ("600001",), NOW + timedelta(days=2), include_structured=True, action_restrictions=restrictions
    )
    assert not later["600001"].corporate_risk_window_complete
    assert "risk_coverage:stale" in later["600001"].source_errors
    assert restrictions == {"600001": {"research_data_degraded"}}
    assert not research.load(("600001",), NOW - timedelta(seconds=1), include_structured=True)


def test_content_removed_without_publication_change_preserves_loaded_memory(tmp_path):
    index = _index(tmp_path)
    source = Sources()
    SyncCompanyEvidence(CompanyEvidenceDependencies(index, source, source, lambda: 0), ("600001",), NOW)
    research = PublishedCompanyResearch(
        SQLiteCompanyEvidenceReader(index.path),
        wall_clock=lambda: NOW,
        open_dates=lambda: PreparedFixture(NOW).open_dates,
        monotonic=time.monotonic,
    )
    research.initialize(("600001", "600002"), cancelled=lambda: False)
    assert research.load(("600001",), NOW, include_structured=True)["600001"].corporate_risk_window_complete
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("DELETE FROM published")
    research.observe_publication()
    retained = research.cached(("600001",), include_structured=True)["600001"]
    assert retained.corporate_risk_window_complete
    assert not research.status().restart_required


def test_access_denied_stops_pdf_requests_and_keeps_checkpoint(tmp_path):
    from trader.download.domain.company_evidence import CompanySourceFailure

    index = _index(tmp_path)
    part = index.partitions(("sse",), NOW)[0]
    index.commit_page(part, DisclosurePage((_disclosure(title="关于本公司被立案调查的公告"),), 2, True), NOW)

    class DeniedSources(Sources):
        def fetch(self, first, **kwargs):
            if isinstance(first, DisclosurePartition):
                raise CompanySourceFailure("access_denied")
            return super().fetch(first, **kwargs)

    source = DeniedSources()
    result = SyncCompanyEvidence(CompanyEvidenceDependencies(index, source, source, lambda: 0), ("600001",), NOW)
    assert "announcements:access_denied" in result.failures
    assert "risk_documents:pending" in result.failures
    assert index.partitions(("sse",), NOW)[0].page == 2


@pytest.mark.parametrize(
    "failure,category",
    [
        (HttpTimeout("private transport message"), "timeout"),
        (CompanySourceFailure("http_error", status_code=503), "http_503"),
    ],
)
def test_announcements_failure_does_not_leak_into_pdf_progress_and_restarts_at_failed_page(tmp_path, failure, category):
    index = _index(tmp_path)
    target = index.partitions(("sse",), NOW)[0]
    events = []

    class Resumable(Sources):
        blocked = True

        def __init__(self):
            super().__init__()
            self.pages = []
            self.documents = []

        def fetch(self, query, **kwargs):
            if not isinstance(query, DisclosurePartition):
                return super().fetch(query, **kwargs)
            if (query.start, query.end) != (target.start, target.end):
                return DisclosurePage((), 0, False)
            self.pages.append(query.page)
            if query.page == 1:
                return DisclosurePage((_disclosure(title="关于本公司被立案调查的公告"),), 2, True)
            if self.blocked:
                raise failure
            return DisclosurePage((_disclosure("second"),), 2, False)

        def document(self, item, **kwargs):
            self.documents.append(item.identity)
            return "已取得正文"

    source = Resumable()
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0, events.append)
    first = SyncCompanyEvidence(dependencies, ("600001",), NOW)
    assert first.failures == (f"announcements:{category}",)
    assert first.pending_partitions > 0 and first.pending_documents == 0
    assert source.pages == [1, 2] and source.documents == ["first"]
    body_events = [event for event in events if event.component == "risk_documents"]
    assert all(not event.failure for event in body_events)
    assert (body_events[-1].completed, body_events[-1].pending, body_events[-1].status) == (1, 0, "complete")
    assert any(event.component == "announcements" and event.scope and event.scope.page == 2 for event in events)

    source.blocked = False
    source.calls.clear()
    events.clear()
    second = SyncCompanyEvidence(dependencies, ("600001",), NOW)
    assert not second.needs_retry
    assert source.pages == [1, 2, 2] and source.documents == ["first"]
    assert not source.calls
    assert all(
        next(event for event in events if event.component == component).status == "reused"
        for component in ("financial", "pledge", "unlock")
    )
    assert SQLiteCompanyEvidenceReader(index.path).read(("600001",))[0].announcement_complete


def test_document_failure_counts_unique_pending_documents_and_retries_only_unfinished_bodies(tmp_path):
    index = _index(tmp_path)
    source = Sources()
    codes = ("600001", "600002")
    SyncCompanyEvidence(CompanyEvidenceDependencies(index, source, source, lambda: 0), codes, NOW)
    part = DisclosurePartition("sse", NOW.date(), NOW.date())
    item = replace(_disclosure(title="关于本公司被立案调查的公告"), codes=codes)
    index.commit_page(part, DisclosurePage((item,), 1, False), NOW)
    events = []

    class Documents(Sources):
        blocked = True
        document_calls = 0

        def document(self, item, **kwargs):
            self.document_calls += 1
            if self.blocked:
                raise HttpTimeout("private transport message")
            return "已取得正文"

    source = Documents()
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0, events.append)
    first = SyncCompanyEvidence(dependencies, codes, NOW)
    assert first.pending_partitions == 0 and first.pending_documents == 1
    assert first.failures == ("risk_documents:pending", "risk_documents:timeout")
    body = next(event for event in events if event.component == "risk_documents")
    assert body.pending == 1  # One disclosure associated with two stocks is still one PDF.
    assert events[-1].pending_documents == 1
    source.blocked = False
    second = SyncCompanyEvidence(dependencies, codes, NOW)
    assert second.pending_documents == 0 and not second.needs_retry
    assert source.document_calls == 2 and not source.calls
    third = SyncCompanyEvidence(dependencies, codes, NOW)
    assert source.document_calls == 2 and not third.needs_retry


@pytest.mark.parametrize("failures_before_success", [1, 3])
def test_cninfo_recovers_same_uncommitted_page_without_replaying_saved_prefix(tmp_path, failures_before_success):
    index = _index(tmp_path)
    target = index.partitions(("sse",), NOW)[0]
    span = f"{target.start.isoformat()}~{target.end.isoformat()}"
    clock = [0.0]
    pages = []
    events = []

    class Transport:
        def post(self, url, **kwargs):
            clock[0] += 0.1
            query = kwargs["data"]
            rows, total, more = [], 0, False
            if query["seDate"] == span:
                page = int(query["pageNum"])
                pages.append(page)
                if page == 2 and pages.count(2) <= failures_before_success:
                    clock[0] += 8
                    raise HttpTimeout("fixture")
                total, more = 2, page == 1
                rows = [
                    {
                        "announcementId": str(page),
                        "secCode": "600001",
                        "announcementTitle": "普通公告",
                        "announcementTime": NOW.timestamp() * 1000,
                        "adjunctUrl": "",
                    }
                ]
            return HttpResponse(
                200, json.dumps({"totalRecordNum": total, "hasMore": more, "announcements": rows}).encode()
            )

    def sleep(seconds):
        clock[0] += seconds

    source = CninfoMarketDisclosureSource(Transport(), monotonic=lambda: clock[0], sleeper=sleep)
    dependencies = CompanyEvidenceDependencies(index, source, Sources(), lambda: clock[0], events.append)
    result = SyncCompanyEvidence(dependencies, ("600001",), NOW)
    news = [event for event in events if event.component == "news"]
    assert len(news) == 1 and news[0].status == "complete" and news[0].pending == 0
    if failures_before_success == 3:
        assert result.failures == ("announcements:timeout",) and result.pending_partitions > 0
        checkpoint = next(
            part for part in index.partitions(("sse",), NOW) if (part.start, part.end) == (target.start, target.end)
        )
        assert (checkpoint.page, checkpoint.received_rows) == (2, 1)
        result = SyncCompanyEvidence(dependencies, ("600001",), NOW)
    assert not result.needs_retry and result.pending_partitions == 0
    assert pages == [1] + [2] * (failures_before_success + 1)
    assert events[-1].status == "complete" and not events[-1].failure
    assert SQLiteCompanyEvidenceReader(index.path).read(("600001",))[0].announcement_complete
