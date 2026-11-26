"""Current inputs can finish independently while directory backfill keeps its cursor."""

from dataclasses import replace
from datetime import timedelta

from tests.download.component.company.test_company_sync_round import _SCOPE
from tests.download.component.company.test_company_sync_runtime import _runtime
from trader.download.domain.company_evidence import Disclosure, DisclosurePage, DisclosurePartition
from trader.download.domain.company_sync import CompanySyncBudget, CompanySyncFamily
from trader.infra.sqlite_connection import SqliteSession


def test_latest_refresh_uses_new_same_day_cutoff_and_keeps_backfill_cursor(tmp_path, monkeypatch):
    runtime, _, transport, requests = _runtime(tmp_path)
    day = _SCOPE.cutoff.date()
    window = DisclosurePartition("sse", day - timedelta(days=365), day - timedelta(days=1))
    doc = Disclosure("cninfo", "accepted-a", _SCOPE.codes, "普通公告", _SCOPE.cutoff - timedelta(days=2), "", "a" * 64)
    budget = CompanySyncBudget(None, None, None, float("inf"))

    def sync(cutoff):
        return runtime.run(_SCOPE.codes, cutoff, budget, monotonic=lambda: 0, cancel_requested=lambda: False,
                           latest_only=True)

    with transport:
        runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
        runtime.material.register_partition(window, _SCOPE.cutoff)
        runtime.material.commit_page(window, DisclosurePage((doc,), 2, True), _SCOPE.cutoff)
        pending = runtime.material.pending_partitions(window)
        first = sync(_SCOPE.cutoff)
        assert first.latest and first.latest.ready and not first.business_complete and not first.needs_retry
        assert first.latest.pledge.accepted_pages == first.latest.unlock.accepted_pages == 1
        first_requests = len(requests)
        repeated = sync(_SCOPE.cutoff)
        assert repeated.latest.ready and len(requests) == first_requests
        old_calls = runtime.journal.source_calls(first.round_id)
        next_cutoff = _SCOPE.cutoff + timedelta(minutes=10)
        refreshed = sync(next_cutoff)
        assert refreshed.latest and refreshed.latest.ready and refreshed.scope.cutoff == next_cutoff
        assert refreshed.round_id != first.round_id and runtime.journal.source_calls(first.round_id) == old_calls
        assert runtime.material.pending_partitions(window) == pending and pending[0].page == 2
        assert runtime.journal.family(refreshed.round_id, CompanySyncFamily.ANNUAL_INCOME).source_calls == 0
        following_day = sync(next_cutoff + timedelta(days=1))
        assert following_day.latest.ready and following_day.scope.cutoff == next_cutoff + timedelta(days=1)
        assert runtime.material.pending_partitions(window) == pending
        with SqliteSession(runtime.material.path, sqlite_policy=runtime._sqlite_policy) as conn:
            assert conn.run("SELECT count(*) FROM disclosures WHERE source_key='accepted-a'").require_one()[0] == 1
        old_fetch = runtime._official_source.fetch
        resumed = []

        def fetch(query, **kwargs):
            page = old_fetch(query, **kwargs)
            if isinstance(query, DisclosurePartition) and query.start == window.start and query.end == window.end:
                resumed.append(query.page)
                return DisclosurePage((replace(doc, identity="accepted-b", digest="b" * 64),), 2, False)
            return page

        monkeypatch.setattr(runtime._official_source, "fetch", fetch)
        runtime.run(_SCOPE.codes, following_day.scope.cutoff, budget, monotonic=lambda: 0,
                    cancel_requested=lambda: False)
        assert resumed == [2] and runtime.material.pending_partitions(window) == ()
