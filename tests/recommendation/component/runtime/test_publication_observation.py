"""New publications signal restart without replacing startup inputs or doing query I/O."""

import threading
import time
from dataclasses import replace
from datetime import timedelta

import pytest

from tests.helpers.download.company.company_startup import _AT, _DATES, _snapshot
from tests.helpers.download.company_resources import SQLiteCompanyEvidenceReaderFixture, SQLiteCompanyFactsIndexFixture
from tests.helpers.download.configuration import CompanyEventPolicyFixture
from tests.helpers.recommendation.company_research import PublishedCompanyResearchFixture
from tests.helpers.recommendation.market.candidate_history_tail import HistoryInput, NOW
from tests.helpers.recommendation.runtime.published_input_preloader import _cache
from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.shared.reference_fixtures import PreparedFixture
from trader.download.domain.company_facts import CompileCompanyFacts
from trader.download.infra.reference_index import SQLiteReferenceIndex
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.infra.shutdown import ShutdownDeadline
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession
from trader.recommendation.infra.market_data.published_input_preloader import PublishedInputPreloader
from trader.recommendation.infra.market_data.published_references import PublishedReferences


class _ObservableHistory(HistoryInput):
    def publication_identity(self, *, cancel_requested=None):
        if cancel_requested is not None and cancel_requested():
            raise InterruptedError("history_observation_cancelled")
        return self.current.snapshot_hash


def test_history_observation_preserves_loaded_projection_and_reports_recovery(monkeypatch):
    source = _ObservableHistory(61, 1, ("600001",))
    history = _cache(source)
    history.initialize(cancelled=lambda: False)
    before = history.entries()
    source.current = replace(source.current, snapshot_hash="b" * 64)
    monkeypatch.setattr(source, "manifest", lambda: pytest.fail("observation loaded history manifest"))
    monkeypatch.setattr(source, "iter_windows", lambda *args, **kwargs: pytest.fail("observation scanned prices"))
    history.observe_publication(lambda: False)
    assert history.status().snapshot_hash == "a" * 64
    assert history.status().disk_snapshot_hash == "b" * 64
    assert history.status().restart_required
    assert history.entries() == before

    def unavailable(**kwargs):
        raise RuntimeError("unreadable")

    with monkeypatch.context() as scope:
        scope.setattr(source, "publication_identity", unavailable)
        history.observe_publication(lambda: False)
    assert history.status().disk_snapshot_hash == "b" * 64
    assert history.status().publication_observation_failure == "history_observation:RuntimeError"
    history.observe_publication(lambda: True)
    assert history.status().publication_observation_failure is not None
    history.observe_publication(lambda: False)
    assert history.status().publication_observation_failure is None


def test_reference_observation_uses_real_pointer_and_status_does_no_io(tmp_path, monkeypatch):
    index = SQLiteReferenceIndex(tmp_path / "reference.sqlite3", sqlite_policy=SqlitePolicyFixture())
    first = PreparedFixture(NOW)
    index.publish(first, first.st_evidence)
    references = PublishedReferences(index.read, index.identity, lambda _at: (), max_age_seconds=3600)
    references.initialize()
    second = PreparedFixture(NOW, tuple(replace(item, name="最新公司名称") for item in first.official_universe))
    index.publish(second, second.st_evidence)
    monkeypatch.setattr(references, "_read", lambda: pytest.fail("observer loaded reference payload"))
    references.observe_publication(lambda: False)
    assert references.reference() is not None and references.reference().identity == first.identity
    status = references.status()
    assert status.loaded_identity == first.identity and status.disk_identity == second.identity
    assert status.restart_required and status.publication_observation_failure is None
    monkeypatch.setattr(references, "_read_identity", lambda **kwargs: pytest.fail("status did I/O"))
    assert references.status() == status
    references.observe_publication(lambda: True)


def test_company_observation_sees_new_commit_without_replacing_loaded_facts(tmp_path, monkeypatch):
    writer = SQLiteCompanyFactsIndexFixture(tmp_path / "company.sqlite3")
    writer.initialize()
    facts = CompileCompanyFacts(_snapshot(), CompanyEventPolicyFixture())
    sources = {"investigation": "cninfo"}
    writer.publish((facts,), sources, None, _AT)
    reader = SQLiteCompanyEvidenceReaderFixture(writer.path)
    research = PublishedCompanyResearchFixture(
        reader, wall_clock=lambda: _AT, open_dates=lambda: _DATES, monotonic=time.monotonic,
    )
    research.initialize((facts.code,), cancelled=lambda: False)
    before = research.cached((facts.code,), include_structured=True, as_of=_AT)
    first_identity = research.status().loaded_identity
    with SqliteSession(writer.path, SqliteConnectionOptions(read_only=True), sqlite_policy=SqlitePolicyFixture()) as connection:
        parent = ReadCompanyPublicationIdentity(connection)
    writer.publish((replace(facts, checked_at=_AT + timedelta(seconds=1), published_at=_AT + timedelta(seconds=1)),), sources, parent, _AT + timedelta(seconds=1))
    monkeypatch.setattr(reader, "visit", lambda *args, **kwargs: pytest.fail("observer reloaded company facts"))
    monkeypatch.setattr(reader, "publication_version", lambda: pytest.fail("observer verified all company facts"))
    research.observe_publication(lambda: False)
    status = research.status()
    assert status.restart_required and status.loaded_identity == first_identity
    assert status.disk_identity != first_identity and status.publication_observation_failure is None
    assert research.cached((facts.code,), include_structured=True, as_of=_AT) == before
    monkeypatch.setattr(reader, "publication_identity", lambda **kwargs: pytest.fail("status did I/O"))
    assert research.status() == status
    research.observe_publication(lambda: True)


def test_owned_worker_observes_after_preload_and_stops_in_same_deadline():
    history = _cache(_ObservableHistory(61, 1, ("600001",)))
    ready, observed = threading.Event(), threading.Event()
    order = []

    def initialize(cancelled):
        history.initialize(cancelled=cancelled)
        order.append("preloaded")
        ready.set()

    def observe(cancelled):
        order.append("observed")
        observed.set()

    preloader = PublishedInputPreloader(history, initialize=initialize, observation_interval_seconds=0.01)
    preloader.register_publication_observer(observe)
    assert preloader.start()
    try:
        assert ready.wait(2) and observed.wait(2)
        assert order[0] == "preloaded" and "observed" in order[1:]
        with pytest.raises(RuntimeError, match="registration is closed"):
            preloader.register_publication_observer(observe)
    finally:
        assert preloader.stop(wait=True, deadline=ShutdownDeadline.start(2)).completed
    assert not preloader.start()


def test_cancellation_during_observation_discards_late_identity(monkeypatch):
    source = _ObservableHistory(61, 1, ("600001",))
    history = _cache(source)
    history.initialize(cancelled=lambda: False)
    cancelled = threading.Event()

    def late_identity(**kwargs):
        cancelled.set()
        return "b" * 64

    monkeypatch.setattr(source, "publication_identity", late_identity)
    history.observe_publication(cancelled.is_set)
    assert history.status().disk_snapshot_hash == "a" * 64
    assert not history.status().restart_required
