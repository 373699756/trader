"""Final transaction admission preserves the last complete batch at market close."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from threading import Event, RLock

import pytest

from tests.unit.domain.test_freeze_identity import NOW, freeze
from trader.recommendation.application.pipeline.freeze_publish import snapshot_publisher
from trader.recommendation.application.pipeline.freeze_publish.current_publisher import (
    PreparedCurrentPair,
    PublishCurrentBatch,
)
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.domain.publication.freeze_identity import FreezeOverlay
from trader.recommendation.domain.publication.models import Strategy


@dataclass
class _Clock:
    current: datetime

    def now(self) -> datetime:
        return self.current


def _pairs(profiles, clock, lock, *, preview=False):
    pairs = []
    for profile in profiles:
        index = UnifiedFreezeIndex(transaction_lock=lock)
        old = (freeze(Strategy.TOMORROW), freeze(Strategy.D25))
        for item in old:
            assert index.publish(item, expected_version=None).accepted
        candidates = tuple(replace(item, sequence=2) for item in old)
        overlays = tuple(
            FreezeOverlay(
                item.strategy,
                item.trade_date,
                item.version,
                item.observed_at,
                (item.items[0].quote,),
                sequence=1,
            )
            for item in candidates
        )
        pairs.append(PreparedCurrentPair(
            profile,
            index,
            candidates,
            overlays,
            tuple(item.version for item in old),
            None,
            (None, None),
            clock,
            None if preview else NOW.replace(hour=15, minute=0),
        ))
    return tuple(pairs)


@pytest.mark.parametrize("profiles", (("v5",), ("v1", "v3"), ("v1", "v2", "v3", "v5")))
@pytest.mark.parametrize("offset", (-1, 0, 1))
def test_close_boundary_accepts_only_before_close_and_preserves_whole_previous_batch(profiles, offset):
    clock = _Clock(NOW.replace(hour=15, minute=0) + timedelta(microseconds=offset))
    pairs = _pairs(profiles, clock, RLock())
    indexes = tuple(pair.index for pair in pairs)
    previous = UnifiedFreezeIndex.snapshot_batch(indexes)
    result = PublishCurrentBatch(pairs)
    assert result.accepted is (offset < 0)
    if offset >= 0:
        assert result.reason == "publication_window_closed" and not result.events
        assert UnifiedFreezeIndex.snapshot_batch(indexes) == previous
    else:
        assert tuple(profile for profile, _ in result.events) == profiles
        assert all(pair.index.snapshot(Strategy.TOMORROW).current == pair.freezes[0] for pair in pairs)


def test_clock_is_checked_after_waiting_for_shared_publication_lock():
    clock = _Clock(NOW)
    lock = RLock()
    pairs = _pairs(("v1", "v5"), clock, lock)
    previous = UnifiedFreezeIndex.snapshot_batch(tuple(pair.index for pair in pairs))
    started = Event()

    def publish():
        started.set()
        return PublishCurrentBatch(pairs)

    with ThreadPoolExecutor(max_workers=1) as worker:
        with lock:
            pending = worker.submit(publish)
            assert started.wait(2)
            assert not pending.done()
            clock.current = NOW.replace(hour=15, minute=0)
        result = pending.result(timeout=2)
    assert not result.accepted and result.reason == "publication_window_closed"
    assert UnifiedFreezeIndex.snapshot_batch(tuple(pair.index for pair in pairs)) == previous


@pytest.mark.parametrize("budget_remaining", (True, False))
def test_preview_has_no_market_close_cutoff_but_retains_round_deadline(budget_remaining):
    pairs = _pairs(("v1", "v5"), _Clock(NOW.replace(hour=16)), RLock(), preview=True)
    result = PublishCurrentBatch(pairs, lambda: budget_remaining)
    assert result.accepted is budget_remaining
    assert result.reason == ("accepted" if budget_remaining else "round_timeout")


def test_any_profile_with_closed_window_rejects_all_profiles():
    pairs = _pairs(("v1", "v5"), _Clock(NOW), RLock())
    pairs = (pairs[0], replace(pairs[1], clock=_Clock(NOW.replace(hour=15, minute=0))))
    previous = UnifiedFreezeIndex.snapshot_batch(tuple(pair.index for pair in pairs))
    assert PublishCurrentBatch(pairs).reason == "publication_window_closed"
    assert UnifiedFreezeIndex.snapshot_batch(tuple(pair.index for pair in pairs)) == previous


@pytest.mark.parametrize("failure", ("close", "event_error"))
def test_all_event_preparation_precedes_final_eligibility_and_every_index_write(monkeypatch, failure):
    clock = _Clock(NOW)
    pairs = _pairs(("v1", "v5"), clock, RLock())
    indexes = tuple(pair.index for pair in pairs)
    previous = UnifiedFreezeIndex.snapshot_batch(indexes)
    build_event = snapshot_publisher.BuildFreezeCommitted
    calls = 0

    def prepare_event(identity, *, projection_version):
        nonlocal calls
        calls += 1
        if calls == 3:
            if failure == "event_error":
                raise ValueError("event_projection_failed")
            clock.current = NOW.replace(hour=15, minute=0)
        return build_event(identity, projection_version=projection_version)

    monkeypatch.setattr(snapshot_publisher, "BuildFreezeCommitted", prepare_event)
    if failure == "event_error":
        with pytest.raises(ValueError, match="event_projection_failed"):
            PublishCurrentBatch(pairs)
    else:
        assert PublishCurrentBatch(pairs).reason == "publication_window_closed"
    assert UnifiedFreezeIndex.snapshot_batch(indexes) == previous
