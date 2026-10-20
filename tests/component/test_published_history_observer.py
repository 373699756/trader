import threading
import time
from dataclasses import replace
from datetime import timedelta

import pytest

from tests.component.test_candidate_history_tail import NOW, _Archive
from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.infra.shutdown import ShutdownDeadline
from trader.recommendation.application.ports.market_data import MarketDataPendingError
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache
from trader.recommendation.infra.market_data.published_history_observer import PublishedHistoryObserver


def _cache(source, *, outcomes=None, monotonic=time.monotonic):
    return PublishedHistoryCache(
        ReadPublishedHistoryUseCase(source),
        lookback_sessions=61,
        monotonic=monotonic,
        outcome_history=ReadPublishedHistoryUseCase(outcomes) if outcomes else None,
    )


def test_observer_loads_once_then_only_reports_new_identity(monkeypatch):
    source = _Archive(61, 1, ("600001",))
    history = _cache(source)
    ready = threading.Event()

    def initialize(cancelled):
        history.initialize(cancelled=cancelled)
        ready.set()

    observer = PublishedHistoryObserver(history, initialize=initialize, interval_seconds=0.01)
    assert observer.start()
    try:
        assert ready.wait(2)
        before = history.entries()
        monkeypatch.setattr(source, "iter_windows", lambda *args, **kwargs: pytest.fail("observer adopted content"))
        source.current = replace(source.current, snapshot_hash="b" * 64)
        history.observe_publication()
        assert history.status().snapshot_hash == "a" * 64
        assert history.status().disk_snapshot_hash == "b" * 64
        assert history.status().restart_required
        assert history.entries() == before
        history.initialize(cancelled=lambda: False)
        assert history.entries() == before
    finally:
        assert observer.stop(wait=True, deadline=ShutdownDeadline.start(2)).completed


def test_first_preload_is_nonblocking_for_readers_and_shutdown_reports_active_io(monkeypatch):
    source = _Archive(61, 1, ("600001",))
    history = _cache(source)
    entered, release = threading.Event(), threading.Event()
    windows = source.iter_windows

    def blocked(manifest, *, sessions):
        entered.set()
        assert release.wait(5)
        yield from windows(manifest, sessions=sessions)

    monkeypatch.setattr(source, "iter_windows", blocked)
    observer = PublishedHistoryObserver(history, initialize=lambda cancelled: history.initialize(cancelled=cancelled))
    observer.start()
    try:
        assert entered.wait(2)
        with pytest.raises(MarketDataPendingError, match="history_projection_loading"):
            history.load(("600001",), deadline=NOW + timedelta(seconds=1))
        report = observer.stop(wait=True, deadline=ShutdownDeadline.start(0.01))
        assert not report.completed and report.timed_out
    finally:
        release.set()
        assert observer.stop(wait=True, deadline=ShutdownDeadline.start(2)).completed
    assert not history.entries()


def test_preload_retries_changed_identity_and_restart_adopts_new_publication(monkeypatch):
    source = _Archive(61, 1, ("600001",))
    history = _cache(source)
    windows = source.iter_windows
    reads = []

    def changed(manifest, *, sessions):
        reads.append(manifest.snapshot_hash)
        yield from windows(manifest, sessions=sessions)
        source.current = replace(source.current, snapshot_hash="b" * 64)

    monkeypatch.setattr(source, "iter_windows", changed)
    history.initialize(cancelled=lambda: False)
    assert reads == ["a" * 64, "b" * 64]
    assert history.status().snapshot_hash == "b" * 64
    source.current = replace(source.current, snapshot_hash="c" * 64)
    monkeypatch.setattr(source, "iter_windows", windows)
    restarted = _cache(source)
    restarted.initialize(cancelled=lambda: False)
    assert restarted.status().snapshot_hash == "c" * 64
    assert history.status().snapshot_hash == "b" * 64


@pytest.mark.parametrize("cancelled", (False, True))
def test_preload_cancel_or_monotonic_deadline_never_accepts_partial_projection(cancelled):
    source = _Archive(61, 1, ("600001", "600002"))
    ticks = iter((0.0, 0.0, 31.0, 32.0, 33.0, 34.0))
    history = _cache(source, monotonic=lambda: next(ticks))
    history.initialize(cancelled=lambda: cancelled)
    assert history.entries() == {}
    assert history.status().maintenance_state == "failed"


def test_missing_outcome_history_does_not_close_online_gate():
    source = _Archive(61, 1, ("600001",))
    history = _cache(source)
    history.initialize(cancelled=lambda: False)
    assert history.cached(("600001",))
    assert history.read_outcome_bars(("600001",), NOW) == {}


def test_missing_short_window_does_not_hide_independent_outcomes(monkeypatch):
    short, full = _Archive(61, 1, ("600001",)), _Archive(61, 1, ("600001",))
    monkeypatch.setattr(short, "manifest", lambda: None)
    history = _cache(short, outcomes=full)
    history.initialize(cancelled=lambda: False)
    assert history.load(("600001",)) == {}
    assert len(history.read_outcome_bars(("600001",), NOW)["600001"]) == 61


def test_failed_identity_observation_preserves_loaded_history(monkeypatch):
    source = _Archive(61, 1, ("600001",))
    history = _cache(source)
    history.initialize(cancelled=lambda: False)
    before = history.entries()

    def unavailable():
        raise RuntimeError("unreadable")

    monkeypatch.setattr(source, "manifest", unavailable)
    history.observe_publication()
    assert history.entries() == before
    assert history.status().maintenance_state == "failed"
    assert history.status().error_count == 1
