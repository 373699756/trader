"""Reference-owned ST stays in memory; non-ST exclusions enter company facts."""

from dataclasses import replace
from datetime import datetime, timedelta
import threading
from zoneinfo import ZoneInfo

import pytest

from trader.download.application.company_exclusions import PublishCompanyExclusions
from tests.helpers.configuration import SQLiteCompanyFactsIndexFixture as SQLiteCompanyFactsIndex
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction
from trader.infra.shutdown import ShutdownDeadline
from trader.infra.workers import BoundedExecutor
from trader.recommendation.domain.market.eligibility import IssuerEligibilityFact, IssuerEligibilityReason
from trader.recommendation.infra.market_data.company_eligibility import CompanyEligibility

AT = datetime(2026, 9, 1, 10, tzinfo=ZoneInfo("Asia/Shanghai"))


def _fact():
    return IssuerEligibilityFact(
        "600001", IssuerEligibilityReason.HISTORICAL_DELISTING_WARNING, AT, "quote:delisting", "market_quote", "a" * 64
    )


def _reader(path):
    return CompanyEligibility(PublishCompanyExclusions(SQLiteCompanyFactsIndex(path)))


def test_non_st_exclusion_is_idempotent_and_keeps_effective_time(tmp_path):
    path = tmp_path / "company_facts.sqlite3"
    index = _reader(path)
    assert index.record((_fact(),)) == 1
    assert index.record((_fact(),)) == 0
    assert index.filter_codes(("600001", "600002"), AT - timedelta(seconds=1)) == ("600001", "600002")
    assert index.filter_codes(("600001", "600002"), AT) == ("600002",)
    assert _reader(path).facts() == (_fact(),)
    with SqliteSession(path) as connection:
        assert tuple(
            connection.run("SELECT reason,source,length(evidence_hash) FROM company_exclusion").require_one()
        ) == (2, 2, 32)


def test_st_observation_is_immediate_but_creates_no_company_database(tmp_path):
    path = tmp_path / "company_facts.sqlite3"
    index = _reader(path)
    fact = replace(_fact(), reason=IssuerEligibilityReason.HISTORICAL_ST)
    index.record((fact,))
    assert index.filter_codes(("600001",), AT) == ()
    assert not path.exists()
    assert _reader(path).facts() == ()


def test_conflicting_evidence_cannot_replace_existing_fact(tmp_path):
    index = _reader(tmp_path / "company_facts.sqlite3")
    index.record((_fact(),))
    with pytest.raises(ValueError, match="conflict"):
        index.record((replace(_fact(), evidence_hash="b" * 64),))
    assert index.facts() == (_fact(),)


def test_retired_company_risk_is_not_reintroduced_as_permanent_exclusion(tmp_path):
    index = _reader(tmp_path / "company_facts.sqlite3")
    with pytest.raises(ValueError, match="reason_inactive"):
        index.record((replace(_fact(), reason=IssuerEligibilityReason.HISTORICAL_AUDITED_LOSS),))
    assert index.facts() == ()


def test_corrupt_exclusion_is_rejected_while_loaded_snapshot_stays_available(tmp_path):
    path = tmp_path / "company_facts.sqlite3"
    index = _reader(path)
    index.record((_fact(),))
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run("PRAGMA ignore_check_constraints=ON")
        connection.run("UPDATE company_exclusion SET reason=99")
    damaged = _reader(path)
    assert not damaged.status().integrity_ok
    assert damaged.filter_codes(("600001",), AT) == ()
    assert index.exclusions(AT)[0].code == "600001"


def test_failed_persistence_keeps_immediate_restriction(tmp_path, monkeypatch):
    index = _reader(tmp_path / "company_facts.sqlite3")

    def fail(facts):
        raise OSError("fixture")

    monkeypatch.setattr(index._publication, "execute", fail)
    with pytest.raises(RuntimeError, match="persistence_failed"):
        index.record((_fact(),))
    assert index.filter_codes(("600001",), AT) == ()
    assert index.status().persistence_error_count == 1
    assert index.status().pending_persistence_count == 1


def test_failed_write_is_retried_even_when_the_fact_is_already_in_memory(tmp_path, monkeypatch):
    path = tmp_path / "company_facts.sqlite3"
    index = _reader(path)
    def fail(facts):
        raise OSError("fixture")

    with monkeypatch.context() as failure:
        failure.setattr(index._publication, "execute", fail)
        with pytest.raises(RuntimeError, match="persistence_failed"):
            index.record((_fact(),))
    assert index.record((_fact(),)) == 0
    assert _reader(path).facts() == (_fact(),)
    assert index.status().pending_persistence_count == 0
    assert index.status().last_error is None


def test_shared_view_and_digest_change_only_for_new_facts_or_effective_boundaries(tmp_path, monkeypatch):
    index = _reader(tmp_path / "company_facts.sqlite3")
    first = replace(_fact(), reason=IssuerEligibilityReason.HISTORICAL_ST)
    future = replace(first, code="600002", effective_at=AT + timedelta(hours=1))
    index.record((first, future))
    ordered = index.facts()
    manifest = index.status().manifest_hash
    view = index.view(AT)

    def unexpected(*args, **kwargs):
        pytest.fail("shared reads must not repeat SQL, sorting or evidence hashing")

    monkeypatch.setattr(index._publication, "read", unexpected)
    monkeypatch.setattr("trader.recommendation.infra.market_data.company_eligibility.IssuerEligibilityFactHash", unexpected)
    for minutes in (0, 5, 20, 59):
        assert index.view(AT + timedelta(minutes=minutes)) is view
        assert index.facts() is ordered
        assert index.status().manifest_hash == manifest
    assert view.st_excluded_codes == {"600001"}
    assert index.view(future.effective_at).st_excluded_codes == {"600001", "600002"}
    assert index.view(AT - timedelta(seconds=1)).st_excluded_codes == set()
    assert index.exclusions(AT)[0].observed_at == AT
    with pytest.raises(TypeError):
        view.reasons["600002"] = first.reason


def test_background_batches_do_not_block_shared_reads_or_duplicate_facts(tmp_path, monkeypatch):
    workers = BoundedExecutor(worker_count=1, queue_capacity=1, thread_name_prefix="eligibility-test")
    path = tmp_path / "company_facts.sqlite3"
    publication = PublishCompanyExclusions(SQLiteCompanyFactsIndex(path))
    index = CompanyEligibility(publication, workers=workers)
    entered, release = threading.Event(), threading.Event()
    batches = []
    original = publication.execute

    def write(facts):
        assert threading.current_thread() is not threading.main_thread()
        batches.append(facts)
        if len(batches) == 1:
            entered.set()
            assert release.wait(3)
        return original(facts)

    monkeypatch.setattr(publication, "execute", write)
    index.record((_fact(),))
    assert not path.exists()
    assert index.start()
    try:
        assert entered.wait(2)
        second = replace(_fact(), code="600002")
        for _ in range(4):
            index.record((_fact(), second))
            assert index.view(AT).st_excluded_codes == {"600001", "600002"}
        assert len(batches) == 1
        assert index.status().pending_persistence_count == 2
    finally:
        release.set()
        assert index.stop(wait=True, deadline=ShutdownDeadline.start(5)).completed
    assert [len(batch) for batch in batches] == [1, 1]
    assert len(_reader(path).facts()) == 2
    assert index.status().pending_persistence_count == 0


def test_background_failure_retries_after_cooldown(tmp_path, monkeypatch):
    workers = BoundedExecutor(worker_count=1, queue_capacity=1, thread_name_prefix="eligibility-retry")
    publication = PublishCompanyExclusions(SQLiteCompanyFactsIndex(tmp_path / "company_facts.sqlite3"))
    ticks = [0.0]
    index = CompanyEligibility(publication, workers=workers, monotonic=lambda: ticks[0])
    attempts = []
    original = publication.execute

    def write(facts):
        attempts.append(facts)
        if len(attempts) == 1:
            raise OSError("fixture")
        return original(facts)

    monkeypatch.setattr(publication, "execute", write)
    index.record((_fact(),))
    index.start()
    try:
        fence = workers.submit(lambda: None)
        assert fence is not None
        fence.result(timeout=3)
        for _ in range(4):
            index.record(())
        assert len(attempts) == 1
        assert index.status().pending_persistence_count == 1
        ticks[0] = 60.0
        index.record(())
    finally:
        assert index.stop(wait=True, deadline=ShutdownDeadline.start(5)).completed
    assert len(attempts) == 2
    assert index.status().persistence_error_count == 1
    assert index.status().last_error is None


def test_conflicting_facts_within_one_batch_do_not_mutate_memory(tmp_path):
    index = _reader(tmp_path / "company_facts.sqlite3")
    with pytest.raises(ValueError, match="conflict"):
        index.record((_fact(), replace(_fact(), evidence_hash="b" * 64)))
    assert index.facts() == ()
    assert index.status().pending_persistence_count == 0


def test_failed_shutdown_reports_unwritten_facts_and_stops_the_shared_worker(tmp_path, monkeypatch):
    workers = BoundedExecutor(worker_count=1, queue_capacity=1, thread_name_prefix="eligibility-stop")
    publication = PublishCompanyExclusions(SQLiteCompanyFactsIndex(tmp_path / "company_facts.sqlite3"))
    index = CompanyEligibility(publication, workers=workers)

    def fail(facts):
        raise OSError("fixture")

    monkeypatch.setattr(publication, "execute", fail)
    index.record((_fact(),))
    index.start()
    report = index.stop(wait=True, deadline=ShutdownDeadline.start(3))
    assert not report.completed
    assert "pending company exclusions: 1" in report.detail
    assert not workers.status().running
    assert index.filter_codes(("600001",), AT) == ()
    assert index.status().pending_persistence_count == 1
    with pytest.raises(RuntimeError, match="stopped"):
        index.record(())


def test_cancelled_batch_leaves_no_partial_exclusion_and_keeps_memory_restriction(tmp_path):
    path = tmp_path / "company_facts.sqlite3"
    publication = PublishCompanyExclusions(SQLiteCompanyFactsIndex(path, cancel_requested=lambda: True))
    index = CompanyEligibility(publication)
    with pytest.raises(RuntimeError, match="persistence_failed"):
        index.record((_fact(),))
    assert _reader(path).facts() == ()
    assert index.filter_codes(("600001",), AT) == ()
    assert index.status().pending_persistence_count == 1
