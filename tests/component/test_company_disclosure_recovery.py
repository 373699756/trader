"""Production use case with durable query leaves and isolated failure scenarios."""

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.component import test_company_evidence as fixtures
from trader.download.application.company_evidence import CompanyEvidenceDependencies
from trader.download.domain.company_evidence import (
    CompanySourceFailure,
    DisclosurePage,
    DisclosurePartition,
    DisclosureQuery,
    DisclosureSecurity,
)
from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex
from trader.infra.http_contracts import HttpCancelled, HttpConnectionError, HttpTimeout
from trader.infra.sqlite_connection import SqliteSession

NOW = fixtures.NOW


def _scope(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    target = index.partitions(("sse",), NOW)[0]
    monkeypatch.setattr(index, "partitions", lambda *_: (target,))
    return index, target


def _run(index, source, now=NOW, **kwargs):
    return fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0), ("600001",), now, **kwargs
    )


def test_date_completes_only_after_both_markets_finish(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    targets = (DisclosurePartition("sse", NOW.date(), NOW.date()), DisclosurePartition("szse", NOW.date(), NOW.date()))
    index.partitions(("sse", "szse"), NOW)
    monkeypatch.setattr(index, "partitions", lambda *_: targets)
    events = []

    class Source(fixtures.Sources):
        def fetch(self, query, **kwargs):
            if isinstance(query, DisclosurePartition):
                dates = [event.dates for event in events if event.component == "announcements" and event.dates]
                assert dates[-1].completed_days == 365
            return super().fetch(query, **kwargs)

    source = Source()
    fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0, events.append), ("600001", "000001"), NOW
    )
    final = [event for event in events if event.component == "announcements"][-1]
    assert final.dates.completed_days == final.dates.total_days == 366


@pytest.mark.parametrize("during_split", (False, True))
def test_cancellation_preserves_cursor_without_source_circuit_or_retry_cooldown(tmp_path, monkeypatch, during_split):
    index, target = _scope(tmp_path, monkeypatch)
    documents = []
    monkeypatch.setattr(
        index,
        "partitions",
        lambda markets, now: tuple(
            part
            for part in SQLiteCompanyEvidenceIndex.partitions(index, markets, now)
            if (part.market, part.start, part.end) == (target.market, target.start, target.end)
        ),
    )

    class Source(fixtures.Sources):
        blocked = True
        cancelled = False

        def fetch(self, query, **kwargs):
            if not isinstance(query, DisclosurePartition):
                return super().fetch(query, **kwargs)
            self.calls.append(query)
            if query.page == 1:
                return DisclosurePage((fixtures._disclosure("prefix", title="收到立案告知书"),), 2, True)
            if self.blocked:
                if during_split:
                    raise ValueError("cninfo_duplicate_page_links")
                self.cancelled = True
                raise HttpCancelled("http_cancelled")
            return DisclosurePage((fixtures._disclosure("tail"),), 2, False)

        def split_queries(self, partition, **kwargs):
            self.cancelled = True
            raise HttpCancelled("http_cancelled")

        def document(self, item):
            documents.append(item.identity)
            return "本公司收到证监会立案告知书，已被立案调查。"

    source = Source()
    dependencies = CompanyEvidenceDependencies(
        index, source, source, lambda: 0, cancel_requested=lambda: source.cancelled
    )
    first = fixtures._sync_company_evidence(dependencies, ("600001",), NOW)
    assert first.failures == ("task:cancelled",) and first.pending_partitions == 1
    assert not documents
    assert index.partition_retry_after(target) is None
    source.blocked = False
    source.cancelled = False
    assert not fixtures._sync_company_evidence(dependencies, ("600001",), NOW).needs_retry
    assert documents == ["prefix"]
    assert [part.page for part in source.calls if isinstance(part, DisclosurePartition)] == [1, 2, 2]


@pytest.mark.parametrize(
    "failure", (ValueError("disclosure_document_no_text"), TimeoutError("disclosure_document_parse_timeout"))
)
def test_document_failure_retains_retry_and_reason(tmp_path, monkeypatch, failure):
    index, _ = _scope(tmp_path, monkeypatch)
    events = []
    clock = [0.0]

    class Source(fixtures.Sources):
        def fetch(self, query, **kwargs):
            if isinstance(query, DisclosurePartition):
                return DisclosurePage((fixtures._disclosure(title="收到立案告知书"),), 1, False)
            return super().fetch(query, **kwargs)

        def document(self, item):
            raise failure

    source = Source()
    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: clock[0], events.append), ("600001",), NOW
    )
    assert result.pending_documents == 1
    assert result.failures == (f"risk_documents:{failure}", "risk_documents:pending")
    assert max(event.failed_documents for event in events) == 1
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT COUNT(*) FROM document_retries").require_one()[0] == 1


def test_oversized_day_completes_only_after_all_board_queries_and_parent_reconcile(tmp_path, monkeypatch):
    index, target = _scope(tmp_path, monkeypatch)
    main = tuple(fixtures._disclosure(str(i)) for i in range(1501))
    star = tuple(replace(fixtures._disclosure(str(i)), codes=("688001",)) for i in range(1501, 3001))

    class Source(fixtures.Sources):
        def fetch(self, query, **kwargs):
            if not isinstance(query, DisclosurePartition):
                return super().fetch(query, **kwargs)
            self.calls.append(query)
            if query.query is None:
                return DisclosurePage(main[:30], 3001, True)
            rows = main if query.query.plate == "shmb" else star
            return DisclosurePage(rows, len(rows), False)

        def split_queries(self, partition, **kwargs):
            return (DisclosureQuery("shmb"), DisclosureQuery("shkcp"))

    source = Source()
    events = []
    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0, events.append), ("600001",), NOW
    )
    assert not result.needs_retry and result.pending_partitions == 0
    queries = [p.query for p in source.calls if isinstance(p, DisclosurePartition)]
    assert queries[0] is None and queries[-1] is None
    assert set(queries[1:-1]) == {DisclosureQuery("shmb"), DisclosureQuery("shkcp")}
    announcement_events = [event for event in events if event.component == "announcements"]
    assert announcement_events[-1].completed == 1 and announcement_events[-1].pending == 0
    days = [event.dates.completed_days for event in announcement_events if event.dates]
    assert days == sorted(days) and days[-2:] == [366, 366]
    assert all(value == 365 for value in days[:-2])
    with SqliteSession(index.path) as c:
        assert (
            c.run(
                "SELECT complete FROM partitions WHERE market=? AND start=? AND end=?",
                (target.market, str(target.start), str(target.end)),
            ).require_one()[0]
            == 1
        )
        assert c.run("SELECT count(*) FROM coverage").require_one()[0] == 1
        assert c.run("SELECT count(*) FROM partition_rows").require_one()[0] == 3001


def test_child_cursor_resume_does_not_replay_parent_or_completed_sibling(tmp_path, monkeypatch):
    index, target = _scope(tmp_path, monkeypatch)
    children = index.split_query(target, (DisclosureQuery("shmb"), DisclosureQuery("shkcp")))
    first, second = children
    item = fixtures._disclosure("prefix")
    index.commit_page(first, DisclosurePage((item,), 2, True), NOW)
    index.commit_page(second, DisclosurePage((), 0, False), NOW)
    resumed = SQLiteCompanyEvidenceIndex(index.path)
    resumed.initialize()
    children = resumed.disclosure_queries(target)
    assert len(children) == 1 and children[0].page == 2
    monkeypatch.setattr(resumed, "partitions", lambda *_: (target,))

    class Source(fixtures.Sources):
        def fetch(self, query, **kwargs):
            if not isinstance(query, DisclosurePartition):
                return super().fetch(query, **kwargs)
            self.calls.append(query)
            if query.query is None:
                return DisclosurePage((item,), 2, True)
            assert query == children[0]
            return DisclosurePage((fixtures._disclosure("tail"),), 2, False)

    source = Source()
    assert not _run(resumed, source).needs_retry
    requests = [p for p in source.calls if isinstance(p, DisclosurePartition)]
    assert len(requests) == 2 and requests[0].page == 2 and requests[1].query is None


@pytest.mark.parametrize("mismatch", ["count", "sample", "overlap"])
def test_incomplete_or_overlapping_query_union_never_publishes_coverage(tmp_path, monkeypatch, mismatch):
    index, target = _scope(tmp_path, monkeypatch)
    children = index.split_query(target, (DisclosureQuery("shmb"), DisclosureQuery("shkcp")))
    one, two = fixtures._disclosure("one"), fixtures._disclosure("two")
    index.commit_page(children[0], DisclosurePage((one,), 1, False), NOW)
    index.commit_page(children[1], DisclosurePage((one if mismatch == "overlap" else two,), 1, False), NOW)
    sample = fixtures._disclosure("missing") if mismatch == "sample" else one
    with pytest.raises(ValueError, match="coverage_mismatch"):
        index.commit_query_coverage(target, DisclosurePage((sample,), 3 if mismatch == "count" else 2, True), NOW)
    with SqliteSession(index.path) as c:
        assert c.run("SELECT count(*) FROM coverage").require_one()[0] == 0
        assert c.run("SELECT count(*) FROM disclosures").require_one()[0] >= 1
    pending = index.disclosure_queries(target)
    if mismatch == "sample":
        assert len(pending) == 2 and all(p.page == 1 for p in pending)
    else:
        assert pending == ()  # Valid child pages survive missing records/overlapping filters.


def test_alias_query_overlap_is_accepted_only_when_exact_union_matches_parent(tmp_path, monkeypatch):
    index, target = _scope(tmp_path, monkeypatch)
    children = index.split_query(
        target,
        (
            DisclosureQuery("shmb", (DisclosureSecurity("600001", "issuer"),)),
            DisclosureQuery("shmb", (DisclosureSecurity("600002", "issuer"),)),
        ),
    )
    rows = (fixtures._disclosure("one"), fixtures._disclosure("two"))
    for child in children:
        index.commit_page(child, DisclosurePage(rows, 2, False), NOW)
    index.commit_query_coverage(target, DisclosurePage(rows[:1], 2, True), NOW)
    with SqliteSession(index.path) as c:
        assert c.run("SELECT count(*) FROM coverage").require_one()[0] == 1
        assert c.run("SELECT count(*) FROM partition_rows").require_one()[0] == 2


def test_missing_historical_issuer_is_added_without_redownloading_completed_queries(tmp_path, monkeypatch):
    index, target = _scope(tmp_path, monkeypatch)
    known = DisclosureQuery("shmb", (DisclosureSecurity("600001", "known"),))
    missing = DisclosureQuery("shmb", (DisclosureSecurity("600002", "historical"),))
    child = index.split_query(target, (known,))[0]
    one, two = fixtures._disclosure("one"), fixtures._disclosure("two")
    index.commit_page(child, DisclosurePage((one,), 1, False), NOW)

    class Source(fixtures.Sources):
        def fetch(self, query, **kwargs):
            if not isinstance(query, DisclosurePartition):
                return super().fetch(query, **kwargs)
            self.calls.append(query)
            assert query.query != known
            return DisclosurePage((two,), 1, False) if query.query else DisclosurePage((one,), 2, True)

        def repair_queries(self, partition, members, **kwargs):
            assert partition == target and members == known.securities and kwargs["missing_rows"] == 1
            return (missing,)

    result = _run(index, Source())
    assert result.pending_partitions == 0 and not result.failures
    with SqliteSession(index.path) as c:
        assert c.run("SELECT count(*) FROM coverage").require_one()[0] == 1
        assert c.run("SELECT sum(complete) FROM disclosure_queries").require_one()[0] == 2


def test_query_policy_releases_only_corrected_failures_once(tmp_path, monkeypatch):
    index, target = _scope(tmp_path, monkeypatch)
    child = index.split_query(target, (DisclosureQuery("shmb", (DisclosureSecurity("600001", "issuer"),)),))[0]
    index.commit_page(child, DisclosurePage((fixtures._disclosure(),), 2, True), NOW)
    index.partition_failure(child, "cninfo_query_filter_ignored")
    index.defer_partition(child, NOW + timedelta(days=1))
    index.partition_failure(target, "HttpConnectionError")
    index.defer_partition(target, NOW + timedelta(minutes=15))
    with SqliteSession(index.path) as c:
        c.run("DELETE FROM disclosure_query_policy")
    index.initialize()
    assert index.partition_retry_after(child) is None
    assert index.partition_retry_after(target) == NOW + timedelta(minutes=15)
    assert index.disclosure_queries(target)[0].page == 2
    index.defer_partition(child, NOW + timedelta(days=1))
    index.initialize()
    assert index.partition_retry_after(child) == NOW + timedelta(days=1)


def test_date_completion_removes_only_withdrawn_links_in_its_market_and_day(tmp_path, monkeypatch):
    index, target = _scope(tmp_path, monkeypatch)
    target = replace(target, start=NOW.date(), end=NOW.date())
    rows = tuple(
        replace(fixtures._disclosure(code), codes=(code,)) for code in ("600001", "900901", "000001", "920001")
    )
    other_day = replace(fixtures._disclosure("yesterday"), published_at=NOW - timedelta(days=1))
    index.commit_news("600001", (*rows, other_day), NOW)
    index.commit_page(target, DisclosurePage((), 0, False), NOW)
    with SqliteSession(index.path) as connection:
        linked = {
            row[0]
            for row in connection.run(
                "SELECT d.source_id FROM disclosures d JOIN disclosure_codes c ON d.id=c.disclosure_id"
            )
        }
    assert linked == {"000001", "920001", "yesterday"}


@pytest.mark.parametrize("error", [HttpTimeout("fixture"), HttpConnectionError("fixture")])
def test_one_announcement_network_failure_does_not_block_other_slices_and_is_cooled(tmp_path, error):
    index = fixtures._index(tmp_path)
    target = index.partitions(("sse",), NOW)[0]

    class Source(fixtures.Sources):
        def fetch(self, query, **kwargs):
            if isinstance(query, DisclosurePartition) and query == target:
                self.calls.append(query)
                raise error
            return super().fetch(query, **kwargs)

    source = Source()
    first = _run(index, source)
    assert first.pending_partitions == 1
    assert len([p for p in source.calls if isinstance(p, DisclosurePartition)]) > 1
    source.calls.clear()
    second = _run(index, source)
    assert second.pending_partitions == 1 and not source.calls


@pytest.mark.parametrize("failures", [3, 5])
def test_consecutive_exhausted_announcement_requests_stop_after_three(tmp_path, failures):
    index = fixtures._index(tmp_path)

    class Source(fixtures.Sources):
        attempts = 0

        def fetch(self, query, **kwargs):
            if isinstance(query, DisclosurePartition):
                self.attempts += 1
                if self.attempts <= failures:
                    raise HttpConnectionError("fixture")
            return super().fetch(query, **kwargs)

    source = Source()
    result = _run(index, source)
    assert source.attempts == 3 and "announcements:source_stopped" in result.failures


@pytest.mark.parametrize(
    "error",
    [
        HttpTimeout("fixture"),
        TimeoutError("disclosure_document_parse_timeout"),
        CompanySourceFailure("http_error", status_code=404),
    ],
)
def test_failed_pdf_does_not_block_healthy_document(tmp_path, error):
    index = fixtures._index(tmp_path)
    target = index.partitions(("sse",), NOW)[0]
    rows = tuple(fixtures._disclosure(str(i), title="立案调查", when=NOW - timedelta(seconds=i)) for i in range(2))
    index.commit_page(target, DisclosurePage(rows, 2, False), NOW)

    class Source(fixtures.Sources):
        documents = []

        def document(self, item, **kwargs):
            self.documents.append(item.identity)
            if item.identity == "0":
                raise error
            return "完整核验正文"

    source = Source()
    result = _run(index, source)
    assert source.documents == ["0", "1"] and result.pending_documents == 1
    assert "risk_documents:source_stopped" not in result.failures


def test_parse_failures_do_not_trip_transport_circuit_but_three_network_failures_do(tmp_path):
    index = fixtures._index(tmp_path)
    target = index.partitions(("sse",), NOW)[0]
    rows = tuple(fixtures._disclosure(str(i), title="立案调查", when=NOW - timedelta(seconds=i)) for i in range(8))
    index.commit_page(target, DisclosurePage(rows, 8, False), NOW)

    class Source(fixtures.Sources):
        documents = []

        def document(self, item, **kwargs):
            self.documents.append(item.identity)
            if int(item.identity) < 4:
                raise TimeoutError("disclosure_document_parse_timeout")
            raise HttpTimeout("fixture")

    source = Source()
    result = _run(index, source)
    assert source.documents == [str(i) for i in range(7)]
    assert result.pending_documents == 8 and "risk_documents:source_stopped" in result.failures


def test_query_group_members_and_progress_survive_reopen_without_mutating_publication(tmp_path):
    index = fixtures._index(tmp_path)
    _run(index, fixtures.Sources())
    target = DisclosurePartition("sse", NOW.date(), NOW.date())
    query = DisclosureQuery("shmb", (DisclosureSecurity("600001", "official"),))
    index.split_query(target, (query,))
    with SqliteSession(index.path) as c:
        before = [tuple(row) for row in c.run("SELECT * FROM published")]
    index.initialize()
    assert index.disclosure_queries(target)[0].query == query
    with SqliteSession(index.path) as c:
        assert [tuple(row) for row in c.run("SELECT * FROM published")] == before


def test_successful_slice_resets_consecutive_failures_and_validation_failures_have_their_own_limit(
    tmp_path, monkeypatch
):
    index = fixtures._index(tmp_path)
    selected = index.partitions(("sse",), NOW)[:6]
    monkeypatch.setattr(index, "partitions", lambda *_: selected)

    class Source(fixtures.Sources):
        attempts = 0
        invalid = False

        def fetch(self, query, **kwargs):
            if not isinstance(query, DisclosurePartition):
                return super().fetch(query, **kwargs)
            self.attempts += 1
            if self.invalid:
                return DisclosurePage((), 1, False)
            if self.attempts % 3:
                raise HttpTimeout("fixture")
            return DisclosurePage((), 0, False)

    source = Source()
    result = _run(index, source)
    assert source.attempts == 6 and result.pending_partitions == 4
    assert "announcements:source_stopped" not in result.failures
    source.attempts, source.invalid = 0, True
    result = _run(index, source, NOW + timedelta(minutes=16))
    assert source.attempts == 3 and "announcements:source_stopped" in result.failures


@pytest.mark.parametrize("category", ["access_denied", "rate_limited"])
def test_pdf_host_rejection_stops_immediately_without_touching_later_documents(tmp_path, category):
    index = fixtures._index(tmp_path)
    target = index.partitions(("sse",), NOW)[0]
    rows = tuple(fixtures._disclosure(str(i), title="立案调查", when=NOW - timedelta(seconds=i)) for i in range(3))
    index.commit_page(target, DisclosurePage(rows, 3, False), NOW)

    class Source(fixtures.Sources):
        attempts = 0

        def document(self, item, **kwargs):
            self.attempts += 1
            raise CompanySourceFailure(category)

    source = Source()
    result = _run(index, source)
    assert source.attempts == 1 and result.pending_documents == 3
    assert "risk_documents:source_stopped" in result.failures


def test_legacy_multiday_page_over_hundred_is_split_as_dates(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    original = next(p for p in index.partitions(("sse",), NOW) if p.start < p.end)
    original = replace(original, page=101, received_rows=3000, expected_rows=4000)
    monkeypatch.setattr(index, "partitions", lambda *_: (original,))

    class Source(fixtures.Sources):
        def fetch(self, query, **kwargs):
            if isinstance(query, DisclosurePartition) and query.page > 100:
                raise ValueError("cninfo_page_limit")
            return super().fetch(query, **kwargs)

    source = Source()
    result = _run(index, source)
    assert result.pending_partitions == 0 and not result.failures
    children = [p for p in source.calls if isinstance(p, DisclosurePartition)]
    assert len(children) == 2 and all(p.page == 1 and p.end - p.start < original.end - original.start for p in children)


def test_audit_discards_completed_child_queries_before_reopening_date(tmp_path):
    index = fixtures._index(tmp_path)
    target = index.partitions(("sse",), NOW)[0]
    children = index.split_query(target, (DisclosureQuery("shmb"), DisclosureQuery("shkcp")))
    for child in children:
        index.commit_page(child, DisclosurePage((), 0, False), NOW)
    index.commit_query_coverage(target, DisclosurePage((), 0, False), NOW)
    assert index.disclosure_queries(target) == ()
    reopened = index.partitions(("sse",), NOW + timedelta(minutes=16))
    assert target in reopened and index.disclosure_queries(target) is None


@pytest.mark.parametrize("phase", ["announcements", "documents"])
def test_cancellation_during_source_io_does_not_persist_a_supplier_failure(tmp_path, phase):
    index = fixtures._index(tmp_path)
    target = index.partitions(("sse",), NOW)[0]
    if phase == "documents":
        index.commit_page(target, DisclosurePage((fixtures._disclosure(title="立案调查"),), 1, False), NOW)
    cancelled = False

    class Source(fixtures.Sources):
        def fetch(self, query, **kwargs):
            nonlocal cancelled
            if phase == "announcements" and isinstance(query, DisclosurePartition):
                cancelled = True
                raise HttpConnectionError("cancelled fixture")
            return super().fetch(query, **kwargs)

        def document(self, item, **kwargs):
            nonlocal cancelled
            cancelled = True
            raise HttpTimeout("cancelled fixture")

    source = Source()
    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0, cancel_requested=lambda: cancelled),
        ("600001",),
        NOW,
    )
    assert result.failures == ("task:cancelled",)
    with SqliteSession(index.path) as c:
        assert c.run("SELECT count(*) FROM document_retries").require_one()[0] == 0
        assert c.run("SELECT count(*) FROM partitions WHERE failure<>''").require_one()[0] == 0
