"""An intraday directory can acquire a row after split queries are complete."""

from dataclasses import replace
from datetime import timedelta

from tests.download.component.company.test_company_sync_round import _SCOPE
from tests.download.component.company.test_company_sync_runtime import _runtime
from trader.download.domain.company_evidence import Disclosure, DisclosurePage, DisclosurePartition, DisclosureQuery
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_connection import SqliteSession


def test_completed_child_union_resumes_parent_from_exact_saved_response(tmp_path):
    runtime, _, transport, requests = _runtime(tmp_path)
    cutoff = _SCOPE.cutoff
    runtime.initialize(_SCOPE.codes, cutoff)
    index = runtime.material
    parent = DisclosurePartition("sse", cutoff.date(), cutoff.date())
    index.register_partition(parent, cutoff)
    child, = index.split_query(parent, (DisclosureQuery("shmb"),))
    rows = tuple(Disclosure("cninfo", f"announcement-{number}", _SCOPE.codes, "普通公告", cutoff, "", f"{number:064x}")
                 for number in range(31))
    index.commit_page(child, DisclosurePage(rows[:30], 30, False), cutoff)
    assert index.disclosure_queries(parent) == ()
    head = DisclosurePage(rows[:30], 31, True)
    with transport:
        assert not index.recover_directory_page(parent, head, cutoff, "wrong")
        assert index.recover_directory_page(parent, head, cutoff, CanonicalDigest(parent))
        assert index.recover_directory_page(parent, head, cutoff, CanonicalDigest(parent))
        assert not requests and index.disclosure_queries(parent) is None
        with SqliteSession(index.path) as db:
            assert db.run("SELECT count(*) FROM coverage").require_one()[0] == 0
            assert db.run("SELECT count(*) FROM disclosure_query_rows").require_one()[0] == 30
        second, = index.pending_partitions(parent)
        assert second == replace(parent, page=2, expected_rows=31, received_rows=30)
        # A separate temporary transport cooldown must not discard the saved parent cursor.
        old_retry = cutoff + timedelta(minutes=15)
        short_retry = cutoff + timedelta(seconds=1)
        index.defer_partition(second, old_retry)
        index.partition_failure(second, "source_timeout")
        assert index.partition_retry(second, drift_retry_by=short_retry).retry_at == old_retry
        index.partition_failure(second, "cninfo_partition_changed")
        assert index.partition_retry(second, drift_retry_by=short_retry).retry_at == short_retry
        assert index.partition_retry(second).retry_at == short_retry
        assert index.disclosure_queries(parent) is None
        index.commit_page(second, DisclosurePage(rows[30:], 31, False), cutoff)
    with SqliteSession(index.path) as db:
        assert db.run("SELECT count(*) FROM coverage").require_one()[0] == 1
        assert db.run("SELECT count(*) FROM disclosure_codes").require_one()[0] == 31
    assert not index.pending_partitions(parent)
