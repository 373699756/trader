"""A date change preserves confirmed work and opens distinct current-day inputs."""

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.download.component.company.test_company_sync_round import _SCOPE
from tests.download.component.company.test_company_sync_runtime import _runtime
from trader.download.domain.company_evidence import CompanyInputValidationFailure, Disclosure, DisclosurePage, DisclosurePartition
from trader.download.domain.company_sync import CompanySyncBudget, CompanySyncFamily, CompanySyncPhasePlan, CompanySyncPhaseState, CompanySyncStepResult
from trader.download.infra import company_field_publication
from trader.infra.http_contracts import HttpRequest
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_connection import SqliteSession


@pytest.mark.parametrize("started", (False, True))
def test_shifted_window_keeps_partial_edges_and_rechecks_the_whole_prior_intraday(tmp_path, started):
    runtime, _, _, _ = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    material = runtime.material
    day = _SCOPE.cutoff.date()
    tomorrow = _SCOPE.cutoff + timedelta(days=1)
    old_history = DisclosurePartition("sse", day - timedelta(days=365), day - timedelta(days=1))
    next_history = DisclosurePartition("sse", day - timedelta(days=364), day)
    old_day = DisclosurePartition("sse", day, day)
    today_row = Disclosure("cninfo", "morning", _SCOPE.codes, "上午披露", _SCOPE.cutoff, "https://example.test/m.pdf", "a"*64)
    material.register_partition(old_day, _SCOPE.cutoff)
    material.commit_page(old_day, DisclosurePage((today_row,), 1, False), _SCOPE.cutoff)
    material.register_partition(old_history, _SCOPE.cutoff)
    if started:
        earlier = replace(today_row, identity="older", published_at=_SCOPE.cutoff-timedelta(days=2), digest="b"*64)
        material.commit_page(old_history, DisclosurePage((earlier,), 2, True), _SCOPE.cutoff)
    assert material.can_retain_backfill(("sse",), day, tomorrow.date()) is not started
    if started:
        with pytest.raises(ValueError, match="requires_prior_cursor"):
            material.register_partition(next_history, tomorrow)
        old, = material.pending_partitions(old_history)
        assert (old.page, old.received_rows) == (2, 1)
        return
    material.register_partition(next_history, tomorrow)
    positions = material.pending_partitions(next_history)
    assert any(partition.start == next_history.start and partition.page == 1 for partition in positions)
    assert old_day in positions
    with SqliteSession(material.path) as db:
        assert db.run("SELECT COUNT(*) FROM coverage WHERE end_date=?", (day.isoformat(),)).require_one()[0] == 0
    material.commit_page(old_day, DisclosurePage((today_row,), 2, True), tomorrow)
    second = replace(old_day, page=2, received_rows=1, expected_rows=2)
    with pytest.raises(CompanyInputValidationFailure, match="duplicate_page_links"):
        material.commit_page(second, DisclosurePage((today_row,), 2, False), tomorrow)
    with SqliteSession(material.path) as db:
        assert db.run("SELECT COUNT(*) FROM coverage WHERE end_date=?", (day.isoformat(),)).require_one()[0] == 0
    material.commit_page(old_day, DisclosurePage((today_row,), 2, True), tomorrow)
    afternoon = replace(today_row, identity="afternoon", published_at=_SCOPE.cutoff+timedelta(hours=2), digest="c"*64)
    material.commit_page(second, DisclosurePage((afternoon,), 2, False), tomorrow)
    with SqliteSession(material.path) as db:
        assert db.run("SELECT checked_at FROM coverage WHERE end_date=?", (day.isoformat(),)).require_one()[0] == tomorrow.isoformat()
    material.register_partition(next_history, tomorrow)
    assert old_day not in material.pending_partitions(next_history)


@pytest.mark.parametrize("pending", ("none", "local", "unknown"))
def test_backfill_handoff_installs_one_new_scope_without_acknowledging_old_requests(tmp_path, pending):
    runtime, _, _, _ = _runtime(tmp_path)
    runtime.initialize(_SCOPE.codes, _SCOPE.cutoff)
    journal = runtime.journal
    plans = tuple(CompanySyncPhasePlan(f, CanonicalDigest(f)) for f in _SCOPE.families)
    old = journal.begin(_SCOPE, plans, max_source_calls=None)
    for family in (CompanySyncFamily.FINANCIAL, CompanySyncFamily.PLEDGE, CompanySyncFamily.UNLOCK, CompanySyncFamily.ANNOUNCEMENTS):
        journal.start(old, family, CompanySyncPhaseState(CanonicalDigest(family), True))
        more = family is CompanySyncFamily.ANNOUNCEMENTS
        journal.finish(old, family, CompanySyncStepResult(not more, more))
    new_scope = replace(_SCOPE, cutoff=_SCOPE.cutoff + timedelta(days=1))
    if pending == "unknown":
        journal.start(old, CompanySyncFamily.ANNOUNCEMENTS, CompanySyncPhaseState(CanonicalDigest("unknown"), True))
        journal.authorize_request(HttpRequest("POST", "https://www.cninfo.com.cn/new/hisAnnouncement/query", 1))
    elif pending == "local":
        journal.start(old, CompanySyncFamily.FIELDS, CompanySyncPhaseState(CanonicalDigest("local"), False))
    assert journal.can_handoff_backfill(old, new_scope.cutoff) == (pending != "unknown")
    if pending != "none":
        with pytest.raises(ValueError, match="handoff_changed"):
            journal.begin(new_scope, plans, max_source_calls=None, after_backfill=old)
        assert journal.active().round_id == old
        assert journal.source_calls(old) == int(pending == "unknown")
        if pending == "unknown":
            return
        journal.finish(old, CompanySyncFamily.FIELDS, CompanySyncStepResult(True, False))
    new = journal.begin(new_scope, plans, max_source_calls=None, after_backfill=old)
    assert new != old and journal.active().round_id == new
    assert journal.family(old, CompanySyncFamily.ANNOUNCEMENTS).result.can_continue
    assert not journal.family(old, CompanySyncFamily.ANNOUNCEMENTS).result.processed_scope_complete


@pytest.mark.parametrize("financial_deferred", (False, True))
def test_changed_local_rules_settle_old_intent_and_reprove_before_new_day(tmp_path, monkeypatch, financial_deferred):
    runtime, _, transport, physical = _runtime(tmp_path)
    scope, journal = _SCOPE, runtime.journal
    runtime.initialize(scope.codes, scope.cutoff)
    tomorrow = scope.cutoff + timedelta(days=1)
    with transport:
        for _ in range(20):
            prior = runtime.run(scope.codes, scope.cutoff, CompanySyncBudget(1, None, 1, 10),
                cancel_requested=lambda: False, monotonic=lambda: 0)
            if (journal.can_handoff_backfill(prior.round_id, tomorrow)
                and runtime.material.can_retain_backfill(("sse",), scope.cutoff.date(), tomorrow.date())):
                break
        else:
            pytest.fail("fixture must reach a confirmed directory page before yearly backfill finishes")
        local = CompanySyncFamily.FIELDS
        journal.start(prior.round_id, local, journal.completed_state(prior.round_id, local))
        calls_before = tuple(physical)
        monkeypatch.setattr(company_field_publication, "COMPANY_FIELD_RULES",
            tuple(pair for pair in company_field_publication.COMPANY_FIELD_RULES if pair[0] != "end_date"))
        published = []
        original = runtime._publish_financial_scoring
        def publish(old_scope, query, eligible_codes):
            published.append(old_scope.cutoff)
            original(old_scope, query, eligible_codes)
        monkeypatch.setattr(runtime, "_publish_financial_scoring", publish)
        if financial_deferred:
            pending = iter((True, False))
            monkeypatch.setattr(runtime.normalized, "has_pending_publication", lambda: next(pending))
        successor = runtime.run(scope.codes, tomorrow, CompanySyncBudget(3, None, 1, 10),
            cancel_requested=lambda: False, monotonic=lambda: 0)
    assert successor.round_id != prior.round_id and successor.scope.cutoff == tomorrow
    assert tuple(physical) == calls_before and journal.outstanding_family(prior.round_id) is None
    assert published == [scope.cutoff] * (2 if financial_deferred else 1)
    assert journal.family(prior.round_id, CompanySyncFamily.ANNOUNCEMENTS).result.can_continue
    with SqliteSession(journal.path) as db:
        assert db.run("SELECT COUNT(*) FROM company_sync_local_plan WHERE round_id=? AND family='fields'",
            (prior.round_id,)).require_one()[0] == 2
        assert [tuple(row) for row in db.run("""SELECT processed,can_continue,accepted FROM company_sync_step
            WHERE round_id=? AND family='fields' ORDER BY step DESC LIMIT 2""", (prior.round_id,))] == [(1, 0, 1), (0, 1, None)]
    with SqliteSession(runtime.facts.path) as formal:
        assert formal.run("SELECT rules_hash FROM company_field_proof_rule").require_one()[0] == company_field_publication.CompanyFieldProofRulesHash()
