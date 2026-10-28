import threading
import time
from dataclasses import replace
from datetime import timedelta

import pytest

from tests.component.test_candidate_history_tail import NOW, HistoryInput
from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.infra.shutdown import ShutdownDeadline
from trader.recommendation.application.ports.market_data import MarketDataPendingError
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache
from trader.recommendation.infra.market_data.published_input_preloader import PublishedInputPreloader


def _cache(source, *, outcomes=None, monotonic=time.monotonic):
    return PublishedHistoryCache(
        ReadPublishedHistoryUseCase(source),
        lookback_sessions=61,
        monotonic=monotonic,
        outcome_history=ReadPublishedHistoryUseCase(outcomes) if outcomes else None,
    )


def test_preloader_finishes_after_one_read_and_new_process_adopts_changed_identity(monkeypatch):
    source = HistoryInput(61, 1, ("600001",))
    history = _cache(source)
    ready = threading.Event()

    def initialize(cancelled):
        history.initialize(cancelled=cancelled)
        ready.set()

    preloader = PublishedInputPreloader(history, initialize=initialize)
    assert preloader.start()
    try:
        assert ready.wait(2)
        assert preloader._thread is not None
        preloader._thread.join(2)
        assert not preloader._thread.is_alive()
        before = history.entries()
        source.current = replace(source.current, snapshot_hash="b" * 64)
        with monkeypatch.context() as scope:
            scope.setattr(source, "manifest", lambda: pytest.fail("finished preloader reread disk identity"))
            scope.setattr(
                source, "iter_windows", lambda *args, **kwargs: pytest.fail("finished preloader read content")
            )
            assert not preloader.start()
            history.initialize(cancelled=lambda: False)
            assert history.cached(("600001",))
            assert history.entries() == before
        assert history.status().snapshot_hash == "a" * 64
        assert history.status().disk_snapshot_hash == "a" * 64
        assert not history.status().restart_required
        restarted = _cache(source)
        restarted.initialize(cancelled=lambda: False)
        assert restarted.status().snapshot_hash == "b" * 64
    finally:
        assert preloader.stop(wait=True, deadline=ShutdownDeadline.start(2)).completed


def test_first_preload_is_nonblocking_for_readers_and_shutdown_reports_pending_io(monkeypatch):
    source = HistoryInput(61, 1, ("600001",))
    history = _cache(source)
    entered, release = threading.Event(), threading.Event()
    windows = source.iter_windows

    def blocked(manifest, *, sessions, check=None):
        entered.set()
        assert release.wait(5)
        yield from windows(manifest, sessions=sessions, check=check)

    monkeypatch.setattr(source, "iter_windows", blocked)
    preloader = PublishedInputPreloader(history, initialize=lambda cancelled: history.initialize(cancelled=cancelled))
    preloader.start()
    try:
        assert entered.wait(2)
        with pytest.raises(MarketDataPendingError, match="history_projection_loading"):
            history.load(("600001",), deadline=NOW + timedelta(seconds=1))
        report = preloader.stop(wait=True, deadline=ShutdownDeadline.start(0.01))
        assert not report.completed and report.timed_out
    finally:
        release.set()
        assert preloader.stop(wait=True, deadline=ShutdownDeadline.start(2)).completed
    assert not history.entries()


def test_preload_retries_changed_identity_and_restart_adopts_new_publication(monkeypatch):
    source = HistoryInput(61, 1, ("600001",))
    history = _cache(source)
    windows = source.iter_windows
    reads = []

    def changed(manifest, *, sessions, check=None):
        reads.append(manifest.snapshot_hash)
        yield from windows(manifest, sessions=sessions, check=check)
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
    source = HistoryInput(61, 1, ("600001", "600002"))
    ticks = iter((0.0, 0.0, 301.0, 302.0, 303.0, 304.0))
    history = _cache(source, monotonic=lambda: next(ticks))
    history.initialize(cancelled=lambda: cancelled)
    assert history.entries() == {}
    assert history.status().maintenance_state == "failed"


def test_preload_exceeding_old_thirty_second_limit_publishes_complete_projection():
    source = HistoryInput(61, 1, ("600001", "600002"))
    ticks = iter((0.0, 0.0, 31.0, 32.0, 33.0, 34.0, 35.0, 36.0))
    history = _cache(source, monotonic=lambda: next(ticks))
    history.initialize(cancelled=lambda: False)
    assert set(history.entries()) == {"600001", "600002"}
    assert history.status().maintenance_state == "ready"
    assert history.status().error_count == 0


def test_missing_outcome_history_does_not_close_online_gate():
    source = HistoryInput(61, 1, ("600001",))
    history = _cache(source)
    history.initialize(cancelled=lambda: False)
    assert history.cached(("600001",))
    assert history.read_outcome_bars(("600001",), NOW) == {}


def test_missing_short_window_does_not_hide_independent_outcomes(monkeypatch):
    short, full = HistoryInput(61, 1, ("600001",)), HistoryInput(61, 1, ("600001",))
    monkeypatch.setattr(short, "manifest", lambda: None)
    history = _cache(short, outcomes=full)
    history.initialize(cancelled=lambda: False)
    assert history.load(("600001",)) == {}
    assert len(history.read_outcome_bars(("600001",), NOW)["600001"]) == 61


def test_finished_preload_keeps_loaded_history_when_disk_becomes_unreadable(monkeypatch):
    source = HistoryInput(61, 1, ("600001",))
    history = _cache(source)
    history.initialize(cancelled=lambda: False)
    before = history.entries()

    def unavailable():
        raise RuntimeError("unreadable")

    monkeypatch.setattr(source, "manifest", unavailable)
    history.initialize(cancelled=lambda: False)
    assert history.cached(("600001",))
    assert history.entries() == before
    assert history.status().maintenance_state == "ready"
    assert history.status().error_count == 0


def test_preload_failure_is_recorded_once_and_thread_finishes():
    history = _cache(HistoryInput(61, 1, ("600001",)))
    calls = []

    def unavailable(cancelled):
        calls.append(cancelled())
        raise OSError("unreadable")

    preloader = PublishedInputPreloader(history, initialize=unavailable)
    assert preloader.start()
    try:
        assert preloader._thread is not None
        preloader._thread.join(2)
        assert not preloader._thread.is_alive()
        assert history.status().maintenance_state == "failed"
        assert history.status().maintenance_reason == "OSError"
        assert history.status().maintenance_stage == "startup_preload"
        assert not preloader.start()
        assert calls == [False]
    finally:
        assert preloader.stop(wait=True, deadline=ShutdownDeadline.start(2)).completed
