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
from trader.recommendation.application.runtime.cadence import PipelineTask, ScheduledPipelineTask
from trader.recommendation.application.runtime.recommendation_round import RecommendationRounds
from trader.recommendation.application.runtime.schedule import MarketPhase
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


@pytest.mark.parametrize("profiles", (("v5",), ("v1", "v5"), ("v1", "v2", "v3", "v5")))
@pytest.mark.parametrize("failure_at", ("event_preparation", "batch_notification"))
def test_publication_exception_releases_peers_and_next_round_can_publish(monkeypatch, profiles, failure_at):
    pairs = _pairs(profiles, _Clock(NOW), RLock())
    indexes = tuple(pair.index for pair in pairs)
    previous = UnifiedFreezeIndex.snapshot_batch(indexes)
    fail = True
    error = ValueError("fixture_publication_failure")
    notifications = []
    original = snapshot_publisher.BuildFreezeCommitted

    def build(identity, *, projection_version):
        if fail and failure_at == "event_preparation" and identity is pairs[-1].freezes[-1]:
            raise error
        return original(identity, projection_version=projection_version)

    def notify(sequence, events):
        if fail and failure_at == "batch_notification":
            raise error
        notifications.append((sequence, events))

    monkeypatch.setattr(snapshot_publisher, "BuildFreezeCommitted", build)
    rounds = RecommendationRounds(monotonic=lambda: 0.0, publish_batch_event=notify)
    tasks = (ScheduledPipelineTask(PipelineTask.SCORE, NOW, MarketPhase.BEFORE_CLOSE),)
    for profile in profiles:
        rounds.register(profile)
    admission = rounds.enter(profiles[0], tasks)
    assert all(rounds.enter(profile, tasks) is admission for profile in profiles[1:])

    with ThreadPoolExecutor(max_workers=len(profiles)) as pool:
        peers = [pool.submit(rounds.stage, pair.profile, admission.sequence, pair) for pair in pairs[:-1]]
        try:
            with rounds._condition:
                assert rounds._condition.wait_for(lambda: len(rounds.status().staged_profiles) == len(peers), timeout=2)
            with pytest.raises(ValueError) as caught:
                rounds.stage(pairs[-1].profile, admission.sequence, pairs[-1])
            assert caught.value is error
            before_commit = failure_at == "event_preparation"
            assert rounds.cancelled(admission.sequence) is before_commit
            results = [future.result(timeout=2) for future in peers]
            assert all(result.accepted is not before_commit for result in results)
            assert all(result.reason == ("round_publication_failed" if before_commit else "accepted")
                       for result in results)
            assert rounds.status().outcome == ("round_publication_failed" if before_commit else "published")
            assert not rounds.status().finished
            assert rounds.enter(profiles[-1], tasks) is None
            if before_commit:
                assert UnifiedFreezeIndex.snapshot_batch(indexes) == previous
            else:
                assert all(pair.index.snapshot(item.strategy).current == item for pair in pairs for item in pair.freezes)
        except BaseException:
            # Keep a broken implementation from leaving test workers asleep for
            # the full production deadline, including with the frozen test clock.
            rounds.withdraw(profiles[-1])
            raise
        finally:
            for profile in profiles:
                rounds.finish(profile, admission.sequence)
        assert rounds.status().finished

        fail = False
        next_pairs = []
        for pair in pairs:
            freezes = tuple(replace(item, sequence=3) for item in pair.freezes)
            overlays = tuple(replace(overlay, parent_version=item.version)
                             for item, overlay in zip(freezes, pair.overlays, strict=True))
            expected = tuple(pair.index.snapshot(item.strategy).current.version for item in pair.freezes)
            next_pairs.append(replace(pair, freezes=freezes, overlays=overlays, expected_versions=expected))
        following = rounds.enter(profiles[0], tasks)
        assert following.sequence == admission.sequence + 1
        assert all(rounds.enter(profile, tasks) is following for profile in profiles[1:])
        try:
            futures = [pool.submit(rounds.stage, pair.profile, following.sequence, pair) for pair in next_pairs]
            assert all(future.result(timeout=2).accepted for future in futures)
        finally:
            for profile in profiles:
                rounds.finish(profile, following.sequence)
    assert rounds.status().finished and rounds.status().outcome == "published"
    assert len(notifications) == 1 and notifications[0][0] == following.sequence
    assert all(pair.index.snapshot(item.strategy).current == item for pair in next_pairs for item in pair.freezes)
