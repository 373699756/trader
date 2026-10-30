"""Shared streaming preload and profile-specific window contracts."""

from dataclasses import replace
from datetime import timedelta
import sqlite3

import pytest

from tests.component.test_candidate_history_tail import CALENDAR, NOW, HistoryInput
from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistoryWindow
from trader.infra.shutdown import ShutdownDeadline
from trader.infra.settings.models import HistoryPreloadSettings
from trader.recommendation.application.ports.market_data import MarketDataPendingError
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache
from trader.recommendation.infra.market_data.published_input_preloader import PublishedInputPreloader
from trader.recommendation.infra.market_data.shared_history_projection import SharedHistoryProjection


def _cache(source, sessions, owner=None, rank=False, sequence=False, calendar=None):
    return PublishedHistoryCache(
        ReadPublishedHistoryUseCase(source),
        lookback_sessions=sessions,
        monotonic=lambda: 0.0,
        open_dates=calendar or (lambda: CALENDAR),
        learned_rank_inputs=rank,
        sequence_inputs=sequence,
        projection_owner=owner if owner is not None else SharedHistoryProjection(
            ReadPublishedHistoryUseCase(source), monotonic=lambda: 0.0,
            settings=HistoryPreloadSettings(warn_after_seconds=300.0),
        ),
    )


def test_shared_scan_matches_independent_profile_projections_and_reuses_retained_bars():
    source = HistoryInput(252, 0, ("600001", "600002"))
    reservations = []
    owner = SharedHistoryProjection(
        ReadPublishedHistoryUseCase(source),
        monotonic=lambda: 0.0,
        reserve_bytes=lambda size: reservations.append(size) or True,
        settings=HistoryPreloadSettings(warn_after_seconds=300.0),
    )
    contracts = ((251, True, False), (251, False, False), (61, False, False), (62, False, True))
    shared = tuple(_cache(source, sessions, owner, rank, sequence) for sessions, rank, sequence in contracts)
    shared[0].initialize(cancelled=lambda: False)
    for profile, (sessions, rank, sequence) in zip(shared, contracts, strict=True):
        reference = _cache(source, sessions, rank=rank, sequence=sequence)
        reference.initialize(cancelled=lambda: False)
        assert profile.entries() == reference.entries()
        assert profile.completed_model_inputs(source.current.universe_codes, NOW) == reference.completed_model_inputs(
            source.current.universe_codes, NOW
        )
        profile.initialize(cancelled=lambda: False)
        assert profile.status().history_scan_count == 1
    assert owner.scan_count == 1 and owner.window_count == 2
    assert len(reservations) == 1 and reservations[0] > 0
    assert shared[0].entries()["600001"].bars is shared[-1].entries()["600001"].bars
    for code in source.current.universe_codes:
        contexts = tuple(profile.entries()[code].context for profile in shared)
        assert contexts[0] is contexts[1]
        assert contexts[0] is not contexts[2] and contexts[2] is not contexts[3]
        assert tuple(context.sample_count for context in contexts) == (251, 251, 61, 62)


def test_batch_model_quality_reads_calendar_once_and_keeps_date_failures():
    source = HistoryInput(252, 0, ("600001", "600002"))
    calls = []

    def calendar():
        calls.append(True)
        return CALENDAR

    cache = _cache(source, 251, rank=True, calendar=calendar)
    cache.initialize(cancelled=lambda: False)
    calls.clear()
    inputs = cache.completed_model_inputs(source.current.universe_codes, NOW)
    assert len(calls) == 1 and all(item.quality.failure is None for item in inputs)
    future = cache.completed_model_inputs(("600001",), NOW.replace(year=2027))[0]
    assert future.quality.failure == "history_model_date_mismatch"


def test_missing_history_does_not_require_a_ready_calendar_for_quality_projection():
    source = HistoryInput(252, 0, ("600001",))

    def unavailable_calendar():
        raise AssertionError("unavailable history must not read calendar")

    cache = _cache(source, 251, calendar=unavailable_calendar)
    assert cache.completed_model_inputs(("600001",), NOW)[0].quality.failure == "history_snapshot_unavailable"


def test_cancelled_shared_preload_publishes_no_partial_profile():
    source = HistoryInput(252, 0, ("600001", "600002"))
    owner = SharedHistoryProjection(ReadPublishedHistoryUseCase(source), monotonic=lambda: 0.0, settings=HistoryPreloadSettings(warn_after_seconds=300.0))
    first, second = _cache(source, 251, owner), _cache(source, 61, owner)
    first.initialize(cancelled=lambda: True)
    assert first.entries() == second.entries() == {}
    assert first.status().maintenance_reason == "history_preload_cancelled"
    with pytest.raises(RuntimeError, match="registration is closed"):
        _cache(source, 61, owner)


@pytest.mark.parametrize("slow_phase", ("reading", "publishing"))
def test_slow_preload_warns_once_and_finishes_without_rescanning(slow_phase, caplog):
    ticks = [0.0]

    class SlowHistory(HistoryInput):
        def iter_windows(self, manifest, *, sessions, check=None):
            if slow_phase == "reading":
                ticks[0] = 301.0
            assert check is not None
            check()
            yield from super().iter_windows(manifest, sessions=sessions, check=check)
            ticks[0] = 602.0 if slow_phase == "reading" else 0.0

    def reserve_shared(_size):
        ticks[0] = 903.0
        return True

    source = SlowHistory(252, 0, ("600001",))
    history = ReadPublishedHistoryUseCase(source)
    owner = SharedHistoryProjection(
        history, monotonic=lambda: ticks[0], settings=HistoryPreloadSettings(warn_after_seconds=300.0),
        reserve_bytes=reserve_shared,
    )
    cache = PublishedHistoryCache(
        history, lookback_sessions=251, projection_owner=owner, monotonic=lambda: ticks[0]
    )
    cache.record_maintenance("loading", stage="reading_published_snapshot")
    with pytest.raises(MarketDataPendingError):
        cache.require_projection()
    cache.initialize(cancelled=lambda: False)
    cache.require_projection()
    assert len(cache.entries()) == 1 and owner.window_count == 1 and owner.scan_count == 1
    assert cache.status().maintenance_reason is None
    assert cache.status().maintenance_total_units == 1
    messages = [record.getMessage() for record in caplog.records if "继续读取" in record.getMessage()]
    assert len(messages) == 1
    assert ("301.000" if slow_phase == "reading" else "903.000") in messages[0]
    cache.initialize(cancelled=lambda: False)
    assert owner.scan_count == 1


def test_manifest_change_fails_without_rescanning_or_mixing_profiles():
    class History(HistoryInput):
        def manifest(self):
            self.calls = getattr(self, "calls", 0) + 1
            if self.calls == 2:
                self.current = replace(self.current, snapshot_hash="b" * 64)
            return self.current

    source = History(252, 0, ("600001",))
    owner = SharedHistoryProjection(ReadPublishedHistoryUseCase(source), monotonic=lambda: 0.0, settings=HistoryPreloadSettings(warn_after_seconds=300.0))
    first, second = _cache(source, 251, owner), _cache(source, 61, owner)
    first.initialize(cancelled=lambda: False)
    assert not first.entries() and not second.entries()
    assert first.status().maintenance_reason == second.status().maintenance_reason == "history_snapshot_changed"
    assert owner.scan_count == 1


@pytest.mark.parametrize("failure", (OSError, ValueError, sqlite3.OperationalError, RuntimeError))
def test_preload_errors_are_reported_without_retrying_for_any_profile(failure):
    class TransientHistory(HistoryInput):
        attempts = 0

        def iter_windows(self, manifest, *, sessions, check=None):
            self.attempts += 1
            raise failure("invalid_history")

    source = TransientHistory(252, 0, ("600001",))
    owner = SharedHistoryProjection(
        ReadPublishedHistoryUseCase(source), monotonic=lambda: 0.0,
        settings=HistoryPreloadSettings(warn_after_seconds=10),
    )
    first, second = _cache(source, 251, owner), _cache(source, 61, owner)
    first.initialize(cancelled=lambda: False)
    second.initialize(cancelled=lambda: False)
    assert source.attempts == 1
    assert not first.entries() and not second.entries()
    assert first.status().maintenance_reason == second.status().maintenance_reason == failure.__name__


def test_cancellation_after_warning_still_stops_without_partial_projection(caplog):
    ticks = [0.0]
    cancelled = [False]

    class SlowHistory(HistoryInput):
        def iter_windows(self, manifest, *, sessions, check=None):
            ticks[0] = 4
            assert check is not None
            check()
            cancelled[0] = True
            yield from super().iter_windows(manifest, sessions=sessions, check=check)

    source = SlowHistory(252, 0, ("600001",))
    owner = SharedHistoryProjection(
        ReadPublishedHistoryUseCase(source), monotonic=lambda: ticks[0],
        settings=HistoryPreloadSettings(warn_after_seconds=3),
    )
    first, second = _cache(source, 251, owner), _cache(source, 61, owner)
    first.initialize(cancelled=lambda: cancelled[0])
    assert owner.scan_count == 1
    assert not first.entries() and not second.entries()
    assert first.status().maintenance_reason == second.status().maintenance_reason == "history_preload_cancelled"
    assert "4.000 秒" in caplog.text and "继续读取" in caplog.text


def test_seed_extension_stops_when_the_original_history_window_is_complete():
    class History(HistoryInput):
        def __init__(self):
            super().__init__(62, 0, ("600001",))
            dates = tuple(
                day
                for offset in reversed(range(2800))
                if (day := self.current.data_cutoff - timedelta(days=offset)).weekday() < 5
            )
            self.current = replace(self.current, calendar_dates=dates)
            self.seed_day = dates[-150]
            self.resume_day = dates[-10]
            self.requested_dates = []

        def window(self, code, dates):
            complete = super().window(code, dates)
            return PublishedHistoryWindow(
                code,
                tuple(
                    cell
                    if cell.trade_date == self.seed_day or cell.trade_date >= self.resume_day
                    else PublishedHistoryCell(code, cell.trade_date, "tencent_inferred_suspended", None, None)
                    for cell in complete.cells
                ),
            )

        def read_range(self, manifest, codes, start, end, *, check=None):
            self.requested_dates.append(tuple(day for day in manifest.calendar_dates if start <= day <= end))
            return super().read_range(manifest, codes, start, end, check=check)

    source = History()
    owner = SharedHistoryProjection(ReadPublishedHistoryUseCase(source), monotonic=lambda: 0.0, settings=HistoryPreloadSettings(warn_after_seconds=300.0))
    cache = _cache(source, 61, owner, calendar=lambda: source.current.calendar_dates)

    cache.initialize(cancelled=lambda: False)

    assert len(source.current.calendar_dates) == 2000
    assert source.requested_dates == [source.current.calendar_dates[-125:-62], source.current.calendar_dates[-188:-125]]
    assert len(set(day for dates in source.requested_dates for day in dates)) == 126
    assert owner.timings.extension_rows == 126
    assert owner.scan_count == 1
    entry = cache.entries()["600001"]
    assert entry.context.sample_count == 61
    assert entry.context.latest_trade_date == source.current.data_cutoff.isoformat()


def test_one_preloader_runs_all_profile_callbacks_and_stops_its_thread():
    source = HistoryInput(252, 0, ("600001",))
    owner = SharedHistoryProjection(ReadPublishedHistoryUseCase(source), monotonic=lambda: 0.0, settings=HistoryPreloadSettings(warn_after_seconds=300.0))
    first, second = _cache(source, 251, owner), _cache(source, 61, owner)
    calls = []

    def initialize(cache, cancelled):
        calls.append(cache)
        cache.initialize(cancelled=cancelled)

    preloader = PublishedInputPreloader(first, initialize=lambda cancelled: initialize(first, cancelled))
    preloader.register(second, lambda cancelled: initialize(second, cancelled))
    assert preloader.start()
    # The finite worker finishes naturally; joining must not cancel before callbacks run.
    preloader._thread.join(timeout=2.0)
    assert preloader.stop(wait=True, deadline=ShutdownDeadline.start(2.0)).completed
    assert calls == [first, second]
    assert owner.scan_count == 1
    assert not preloader.start()


def test_one_profile_capacity_failure_does_not_discard_other_valid_projection():
    source = HistoryInput(252, 0, ("600001",))
    history = ReadPublishedHistoryUseCase(source)
    owner = SharedHistoryProjection(history, monotonic=lambda: 0.0, reserve_bytes=lambda _size: True, settings=HistoryPreloadSettings(warn_after_seconds=300.0))
    first = PublishedHistoryCache(
        history, lookback_sessions=251, monotonic=lambda: 0.0, reserve_bytes=lambda _size: False, projection_owner=owner
    )
    second = _cache(source, 61, owner)
    first.initialize(cancelled=lambda: False)
    assert first.entries() == {}
    assert first.status().maintenance_reason == "history_preload_capacity"
    assert second.loaded_identity == source.current.snapshot_hash
    assert owner.scan_count == 1


@pytest.mark.parametrize("cancel_after_reserve", (False, True))
def test_all_failed_profiles_release_shared_bars_reservation(cancel_after_reserve):
    source = HistoryInput(252, 0, ("600001",))
    history = ReadPublishedHistoryUseCase(source)
    reservations = []
    cancelled = [False]

    def reserve(size):
        reservations.append(size)
        cancelled[0] = cancel_after_reserve
        return True

    owner = SharedHistoryProjection(history, monotonic=lambda: 0.0, reserve_bytes=reserve, settings=HistoryPreloadSettings(warn_after_seconds=300.0))
    cache = PublishedHistoryCache(
        history, lookback_sessions=61, monotonic=lambda: 0.0, reserve_bytes=lambda _size: False, projection_owner=owner
    )
    cache.initialize(cancelled=lambda: cancelled[0])
    assert cache.entries() == {}
    assert reservations[0] > 0 and reservations[-1] == 0


def test_same_source_outcomes_remain_readable_when_online_projection_exceeds_capacity():
    source = HistoryInput(252, 0, ("600001",))
    history = ReadPublishedHistoryUseCase(source)
    cache = PublishedHistoryCache(
        history, outcome_history=history, lookback_sessions=61, monotonic=lambda: 0.0, reserve_bytes=lambda _size: False,
        projection_owner=SharedHistoryProjection(
            history, monotonic=lambda: 0.0,
            settings=HistoryPreloadSettings(warn_after_seconds=300.0),
        ),
    )
    cache.initialize(cancelled=lambda: False)
    assert cache.entries() == {}
    assert cache.status().outcome_snapshot_hash == source.current.snapshot_hash
    assert len(cache.read_outcome_bars(("600001",), NOW)["600001"]) == 61
