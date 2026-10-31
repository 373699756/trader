from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from itertools import combinations
import gc
import weakref

import pytest

from trader.recommendation.application.runtime.cadence import PipelineTask, ScheduledPipelineTask
from trader.recommendation.application.runtime.recommendation_round import RecommendationRounds
from trader.recommendation.application.runtime.schedule import SHANGHAI, MarketPhase


def _tasks() -> tuple[ScheduledPipelineTask, ...]:
    return (
        ScheduledPipelineTask(
            PipelineTask.FULL_MARKET,
            datetime(2026, 8, 12, 10, tzinfo=SHANGHAI),
            MarketPhase.BEFORE_CLOSE,
        ),
    )


def test_reservations_finish_before_round_deadline_starts_and_old_inputs_are_released():
    now = [0.0]
    rounds = RecommendationRounds(monotonic=lambda: now[0])

    def prepare():
        now[0] += 5.0

    rounds.register("v1", prepare)
    rounds.register("v5", prepare)
    retained = []
    for _ in range(100):
        before = now[0]
        admission = rounds.enter("v1", _tasks())
        assert admission.started_monotonic == before + 10.0
        assert rounds.enter("v5", _tasks()) is admission
        context = rounds.inputs("v1", admission.sequence)
        retained.append(weakref.ref(context.inputs))
        rounds.finish("v1", admission.sequence)
        rounds.finish("v5", admission.sequence)
        del context
    gc.collect()
    assert all(reference() is None for reference in retained)


def test_all_profiles_share_one_admission_and_absolute_deadline() -> None:
    now = [10.0]
    rounds = RecommendationRounds(monotonic=lambda: now[0])
    for profile in ("v1", "v2", "v3", "v5"):
        rounds.register(profile)
    admissions = [rounds.enter(profile, _tasks()) for profile in ("v1", "v2", "v3", "v5")]
    first = admissions[0]
    assert first is not None and all(item is first for item in admissions)
    assert first.started_monotonic == 10.0
    assert first.deadline_monotonic == 130.0
    now[0] = 129.0
    assert rounds.remaining_seconds(first.sequence) == 1.0


def test_collection_waits_for_every_profile_without_resetting_deadline() -> None:
    rounds = RecommendationRounds()
    rounds.register("v2")
    rounds.register("v3")
    first = rounds.enter("v2", _tasks())
    assert first is not None
    assert rounds.enter("v3", _tasks()) is first
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(rounds.prepared, "v2", first.sequence)
        assert not waiting.done()
        assert rounds.prepared("v3", first.sequence)
        assert waiting.result(timeout=2)


def test_timeout_revokes_publish_eligibility_but_does_not_overlap_running_profile() -> None:
    now = [0.0]
    rounds = RecommendationRounds(monotonic=lambda: now[0])
    for profile in ("v1", "v2", "v3"):
        rounds.register(profile)
    first = rounds.enter("v1", _tasks())
    assert first is not None
    assert rounds.enter("v2", _tasks()) is first
    now[0] = 120.0
    assert not rounds.collected("v1", first.sequence)
    rounds.finish("v1", first.sequence)
    assert rounds.enter("v1", _tasks()) is None
    rounds.finish("v2", first.sequence)
    following = rounds.enter("v1", _tasks())
    assert following is not None and following.sequence > first.sequence


def test_withdrawn_profile_releases_cancelled_round_after_running_work_exits() -> None:
    rounds = RecommendationRounds()
    rounds.register("v1")
    rounds.register("v5")
    first = rounds.enter("v1", _tasks())
    assert first is not None
    rounds.withdraw("v5")
    assert rounds.cancelled(first.sequence)
    rounds.finish("v1", first.sequence)
    following = rounds.enter("v1", _tasks())
    assert following is not None and following.sequence > first.sequence


@pytest.mark.parametrize("elapsed", (0.0, 121.0, 1000.0))
def test_failed_profile_and_absent_peer_do_not_permanently_block_recovery(elapsed: float) -> None:
    now = [0.0]
    rounds = RecommendationRounds(monotonic=lambda: now[0])
    rounds.register("v1")
    rounds.register("v5")
    first = rounds.enter("v1", _tasks())
    assert first is not None
    rounds.finish("v1", first.sequence)
    now[0] = elapsed
    following = rounds.enter("v5", _tasks())
    assert following is not None and following.sequence > first.sequence
    assert rounds.enter("v1", _tasks()) is following


@pytest.mark.parametrize(
    "profiles", [group for size in range(1, 5) for group in combinations(("v1", "v2", "v3", "v5"), size)]
)
def test_only_enabled_profiles_participate_in_collection(profiles: tuple[str, ...]) -> None:
    rounds = RecommendationRounds()
    for profile in profiles:
        rounds.register(profile)
    admissions = [rounds.enter(profile, _tasks()) for profile in profiles]
    first = admissions[0]
    assert first is not None and all(item is first for item in admissions)
    with ThreadPoolExecutor(max_workers=len(profiles)) as pool:
        futures = [pool.submit(rounds.collected, profile, first.sequence) for profile in profiles]
        assert all(future.result(timeout=2.0) for future in futures)


def test_failure_wakes_waiting_peers_without_waiting_for_deadline() -> None:
    rounds = RecommendationRounds()
    for profile in ("v1", "v5"):
        rounds.register(profile)
    admission = rounds.enter("v1", _tasks())
    assert admission is not None and rounds.enter("v5", _tasks()) is admission
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(rounds.prepared, "v1", admission.sequence)
        rounds.finish("v5", admission.sequence)
        assert waiting.result(timeout=2.0) is False
        assert rounds.enter("v5", _tasks()) is None
        rounds.finish("v1", admission.sequence)
    following = rounds.enter("v1", _tasks())
    assert following is not None and following.sequence > admission.sequence
