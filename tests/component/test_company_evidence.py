"""Fresh SQLite, production use case and reader contracts without supplier writes."""

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.download.application.company_evidence import CompanyEvidenceDependencies, SyncCompanyEvidence
from trader.download.domain.company_evidence import (
    CompanyRatio,
    Disclosure,
    DisclosurePage,
    DisclosurePartition,
    StructuredCompanyPage,
    StructuredCompanyQuery,
)
from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex, SQLiteCompanyEvidenceReader
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
    with sqlite3.connect(index.path) as connection:
        connection.execute(
            "CREATE TRIGGER refuse_cursor BEFORE UPDATE ON partitions BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
    with pytest.raises(RuntimeError, match="persistence_unavailable"):
        index.commit_page(partition, DisclosurePage((_disclosure(),), 1, False), NOW)
    with sqlite3.connect(index.path) as connection:
        assert connection.execute("SELECT count(*) FROM disclosures").fetchone()[0] == 0
        assert connection.execute("SELECT MAX(next_page) FROM partitions").fetchone()[0] == 1


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
    with sqlite3.connect(index.path) as connection:
        assert connection.execute("SELECT body,body_checked FROM disclosures").fetchone() == ("verified body", 1)
    changed = replace(item, title="更正公告", digest="b")
    assert index.commit_page(partition, DisclosurePage((changed,), 1, False), NOW) == 1
    with sqlite3.connect(index.path) as connection:
        assert connection.execute("SELECT body,body_checked FROM disclosures").fetchone() == ("", 0)


def test_partial_page_never_removes_existing_links_and_truncation_cannot_complete(tmp_path):
    index = _index(tmp_path)
    partition = index.partitions(("sse",), NOW)[0]
    index.commit_page(partition, DisclosurePage((_disclosure(),), 1, False), NOW)
    with pytest.raises(ValueError, match="truncated"):
        index.commit_page(partition, DisclosurePage((), 1, False), NOW)
    with sqlite3.connect(index.path) as connection:
        assert connection.execute("SELECT count(*) FROM disclosure_codes").fetchone()[0] == 1


def test_publication_isolated_from_unpublished_changes_and_failure(tmp_path):
    index = _index(tmp_path)
    partition = index.partitions(("sse",), NOW)[0]
    index.commit_page(partition, DisclosurePage((_disclosure(),), 1, False), NOW)
    index.publish(("600001",), NOW)
    reader = SQLiteCompanyEvidenceReader(index.path)
    before = reader.read(("600001",))
    index.commit_page(partition, DisclosurePage((_disclosure(title="修订", digest="b"),), 1, False), NOW)
    assert reader.read(("600001",)) == before
    with sqlite3.connect(index.path) as connection:
        connection.execute(
            "CREATE TRIGGER refuse_publication BEFORE UPDATE ON published BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
    with pytest.raises(RuntimeError, match="persistence_unavailable"):
        index.publish(("600001",), NOW)
    assert reader.read(("600001",)) == before


def test_reader_does_not_initialize_or_write_missing_database(tmp_path):
    reader = SQLiteCompanyEvidenceReader(tmp_path / "missing.sqlite3")
    research = PublishedCompanyResearch(reader, wall_clock=lambda: NOW)
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
            with sqlite3.connect(index.path, timeout=0) as connection:
                assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        observed.append(snapshot)

    reader.visit(codes, consume)
    assert tuple(observed) == before
    assert reader.read(codes) != before


def test_company_observer_adopts_manual_publication_without_supplier_or_restart(tmp_path):
    index = _index(tmp_path)
    source = Sources()
    SyncCompanyEvidence(CompanyEvidenceDependencies(index, source, source, lambda: 0), ("600001",), NOW)
    research = PublishedCompanyResearch(SQLiteCompanyEvidenceReader(index.path), wall_clock=lambda: NOW)
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
    assert after != before
    assert after["600001"].pledge_ratio_pct == 80


def test_company_observer_retries_missing_codes_after_later_publication(tmp_path):
    index = _index(tmp_path)
    index.publish(("600001",), NOW)
    research = PublishedCompanyResearch(SQLiteCompanyEvidenceReader(index.path), wall_clock=lambda: NOW)
    assert not research.load(("600002",), NOW, include_structured=True)
    research.observe_publication()
    index.publish(("600002",), NOW)
    research.observe_publication()
    assert research.status().entries == 1


def test_company_observer_recovers_transient_read_error_with_unchanged_version(tmp_path, monkeypatch):
    index = _index(tmp_path)
    index.publish(("600001",), NOW)
    reader = SQLiteCompanyEvidenceReader(index.path)
    research = PublishedCompanyResearch(reader, wall_clock=lambda: NOW)
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
        SQLiteCompanyEvidenceReader(index.path), wall_clock=lambda: NOW, refresh=forbidden
    )
    observations = research.load(("600001",), NOW, include_structured=True)
    assert observations["600001"].corporate_risk_window_complete
    assert research.cached(("600001",), include_structured=True) == observations


def test_old_database_is_rejected_without_mutation(tmp_path):
    path = tmp_path / "company_evidence.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE risk_evidence_recent(payload TEXT)")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="contract_mismatch"):
        SQLiteCompanyEvidenceIndex(path).initialize()
    assert path.read_bytes() == before


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
    with sqlite3.connect(index.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM coverage").fetchone()[0] == 0
    assert index.partitions(("sse",), NOW)[0].page == 1


def test_unchanged_publication_does_not_rewrite_rows_or_change_version(tmp_path):
    index = _index(tmp_path)
    source = Sources()
    SyncCompanyEvidence(CompanyEvidenceDependencies(index, source, source, lambda: 0), ("600001",), NOW)
    reader = SQLiteCompanyEvidenceReader(index.path)
    before = reader.read(("600001",))
    with sqlite3.connect(index.path) as connection:
        connection.execute(
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
    with sqlite3.connect(index.path) as connection:
        assert connection.execute("SELECT ratio_pct FROM ratios").fetchone()[0] == 10
    with pytest.raises(ValueError, match="incomplete"):
        index.commit_structured(
            replace(query, page=2, received_rows=1, expected_rows=2), StructuredCompanyPage("unlock", 2, 2, 2), NOW
        )
    index.commit_structured(query, StructuredCompanyPage("unlock", 1, 1, 0), NOW)
    with sqlite3.connect(index.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ratios").fetchone()[0] == 0


def test_corrupt_read_model_is_rejected_and_previous_memory_is_retained(tmp_path):
    index = _index(tmp_path)
    index.publish(("600001",), NOW)
    reader = SQLiteCompanyEvidenceReader(index.path)
    research = PublishedCompanyResearch(reader, wall_clock=lambda: NOW)
    before = research.load(("600001",), NOW, include_structured=True)
    with sqlite3.connect(index.path) as connection:
        connection.execute("UPDATE published SET payload=replace(payload,'window_incomplete','unexpected')")
    with pytest.raises(ValueError, match="digest"):
        reader.read(("600001",))
    report = research.load_report(("600001",), NOW, include_structured=True)
    assert report.observations["600001"].evidence == before["600001"].evidence
    assert "company_snapshot:ValueError" in report.observations["600001"].source_errors
    assert not report.observations["600001"].corporate_risk_window_complete
    assert report.deferred_codes == ("600001",)


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
    research = PublishedCompanyResearch(reader, wall_clock=lambda: NOW)
    assert research.load(("600001",), NOW, include_structured=True)["600001"].corporate_risk_window_complete

    def forbidden(*args, **kwargs):
        raise AssertionError("cached research must not repeat database reads")

    monkeypatch.setattr(reader, "visit", forbidden)
    restrictions = {}
    later = research.load(
        ("600001",), NOW + timedelta(days=1), include_structured=True, action_restrictions=restrictions
    )
    assert not later["600001"].corporate_risk_window_complete
    assert "company_snapshot:stale" in later["600001"].source_errors
    assert restrictions == {"600001": {"research_data_degraded"}}
    assert not research.load(("600001",), NOW - timedelta(seconds=1), include_structured=True)


def test_missing_published_row_invalidates_existing_memory_coverage(tmp_path):
    index = _index(tmp_path)
    source = Sources()
    SyncCompanyEvidence(CompanyEvidenceDependencies(index, source, source, lambda: 0), ("600001",), NOW)
    research = PublishedCompanyResearch(SQLiteCompanyEvidenceReader(index.path), wall_clock=lambda: NOW)
    assert research.load(("600001",), NOW, include_structured=True)["600001"].corporate_risk_window_complete
    with sqlite3.connect(index.path) as connection:
        connection.execute("DELETE FROM published")
    report = research.load_report(("600001",), NOW, include_structured=True)
    assert report.deferred_codes == ("600001",)
    retained = research.cached(("600001",), include_structured=True)["600001"]
    assert not retained.corporate_risk_window_complete
    assert "company_snapshot:missing" in retained.source_errors


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
