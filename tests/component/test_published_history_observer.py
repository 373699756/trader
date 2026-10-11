from __future__ import annotations

import threading
from dataclasses import replace
from datetime import timedelta

import pytest

from tests.component.test_candidate_history_tail import NOW, _Archive
from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.recommendation.application.ports.market_data import MarketDataUnavailableError
from trader.infra.shutdown import ShutdownDeadline
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache
from trader.recommendation.infra.market_data.published_history_observer import PublishedHistoryObserver


@pytest.mark.parametrize("previous_projection", (False, True))
def test_observer_keeps_existing_projection_readable_and_reports_a_blocked_shutdown(
    monkeypatch, previous_projection
) -> None:
    archive = _Archive(61, 1, ("600001",))
    history = PublishedHistoryCache(ReadPublishedHistoryUseCase(archive), lookback_sessions=61)
    if previous_projection:
        assert history.refresh()
    before = history.status().snapshot_hash
    entered, release = threading.Event(), threading.Event()
    windows = archive.iter_windows
    archive.current = replace(archive.current, snapshot_hash="b" * 64)

    def blocked_windows(manifest, *, sessions):
        entered.set()
        assert release.wait(5)
        yield from windows(manifest, sessions=sessions)

    monkeypatch.setattr(archive, "iter_windows", blocked_windows)
    observer = PublishedHistoryObserver(history)
    assert observer.start()
    try:
        assert entered.wait(2)
        assert history.status().maintenance_state == "loading"
        if previous_projection:
            assert history.load(("600001",), deadline=NOW + timedelta(seconds=1))
        else:
            with pytest.raises(MarketDataUnavailableError, match="history_projection_loading"):
                history.load(("600001",), deadline=NOW + timedelta(seconds=1))
        assert history.status().snapshot_hash == before
        report = observer.stop(wait=True, deadline=ShutdownDeadline.start(0.01))
        assert not report.completed
        assert report.timed_out
    finally:
        release.set()
        assert observer.stop(wait=True, deadline=ShutdownDeadline.start(2)).completed
    assert history.status().snapshot_hash == "b" * 64


@pytest.mark.parametrize("changed_snapshot", (False, True))
def test_observer_adopts_later_publication_retains_failure_and_recovers(monkeypatch, changed_snapshot) -> None:
    archive = _Archive(61, 1, ("600001",))
    history = PublishedHistoryCache(ReadPublishedHistoryUseCase(archive), lookback_sessions=61)
    observer = PublishedHistoryObserver(history, interval_seconds=0.01)
    ready, failed, recovered = threading.Event(), threading.Event(), threading.Event()
    record = history.record_maintenance
    original = archive.manifest
    unavailable = threading.Event()

    def manifest():
        if unavailable.is_set():
            raise RuntimeError("fixture read failure")
        return original()

    def record_and_signal(state, reason=None, **details):
        record(state, reason, **details)
        if state == "failed":
            failed.set()
        if state == "ready":
            ready.set()
            if failed.is_set() and not unavailable.is_set():
                recovered.set()

    monkeypatch.setattr(archive, "manifest", manifest)
    monkeypatch.setattr(history, "record_maintenance", record_and_signal)
    observer.start()
    try:
        assert ready.wait(2)
        before = history.entries()
        unavailable.set()
        assert failed.wait(2)
        assert history.entries() == before
        assert history.status().maintenance_reason == "RuntimeError"
        if changed_snapshot:
            archive.current = replace(archive.current, snapshot_hash="b" * 64)
        unavailable.clear()
        assert recovered.wait(2)
        assert history.status().snapshot_hash == ("b" * 64 if changed_snapshot else "a" * 64)
        assert history.status().maintenance_reason is None
        assert history.status().error_count >= 1
        assert history.cached(("600001",))
    finally:
        assert observer.stop(wait=True, deadline=ShutdownDeadline.start(2)).completed


def test_read_health_preserves_outcome_failure_and_distinguishes_missing_from_invalid_snapshot(
    monkeypatch,
) -> None:
    archive = _Archive(61, 1, ("600001",))
    history = PublishedHistoryCache(ReadPublishedHistoryUseCase(archive), lookback_sessions=61)
    assert history.refresh()

    def unavailable_outcomes(*_args, **_kwargs):
        raise RuntimeError("fixture outcome failure")

    monkeypatch.setattr(archive, "read_windows", unavailable_outcomes)
    assert history.read_outcome_bars(("600001",), NOW) == {}
    history.record_projection_observation()
    assert history.status().maintenance_state == "failed"
    assert history.status().maintenance_reason == "RuntimeError"
    assert history.cached(("600001",))

    missing = PublishedHistoryCache(ReadPublishedHistoryUseCase(archive), lookback_sessions=61)
    monkeypatch.setattr(archive, "manifest", lambda: None)
    missing.refresh()
    missing.record_projection_observation()
    assert missing.status().maintenance_state == "unavailable"
    assert missing.status().maintenance_reason == "history_snapshot_unavailable"

    def invalid_manifest():
        raise RuntimeError("fixture invalid control")

    monkeypatch.setattr(archive, "manifest", invalid_manifest)
    missing.refresh()
    missing.record_projection_observation()
    assert missing.status().maintenance_state == "failed"
    assert missing.status().maintenance_reason == "RuntimeError"


def test_observer_uses_qfq_st_eligibility_without_reading_training_history(monkeypatch) -> None:
    qfq = _Archive(61, 1, ("600001",))
    training = _Archive(61, 1, ("600001",))
    monkeypatch.setattr(training, "manifest", lambda: pytest.fail("online eligibility must not read full history"))
    history = PublishedHistoryCache(
        ReadPublishedHistoryUseCase(qfq),
        lookback_sessions=61,
        outcome_history=ReadPublishedHistoryUseCase(training),
    )
    assert history.refresh()
    assert history.historical_st_eligibility().eligible_codes == ("600001",)

    qfq.current = replace(qfq.current, snapshot_hash="b" * 64, universe_codes=("300001", "600001"))

    assert history.refresh()
    assert history.historical_st_eligibility().source_identity == "b" * 64
    assert history.historical_st_eligibility().eligible_codes == ("300001", "600001")
