from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from trader.recommendation.application.runtime.cadence import PipelineTask, ScheduledPipelineTask
from trader.recommendation.application.runtime.recommendation_round import RecommendationRounds
from trader.recommendation.application.runtime.schedule import SHANGHAI, MarketPhase


def test_all_profiles_finish_collection_and_publication_before_next_wave() -> None:
    rounds = RecommendationRounds()
    for profile in ("v1", "v2", "v3", "v5"):
        rounds.register(profile)
    tasks = (ScheduledPipelineTask(PipelineTask.FULL_MARKET, datetime(2026, 8, 12, 10, tzinfo=SHANGHAI), MarketPhase.BEFORE_CLOSE),)
    admissions = [rounds.enter(profile, tasks) for profile in ("v1", "v2", "v3", "v5")]
    first = admissions[0]
    assert first is not None and all(item is first for item in admissions)
    with ThreadPoolExecutor(max_workers=3) as pool:
        waiting = [pool.submit(rounds.collected, profile, first.sequence, timeout_seconds=2) for profile in ("v1", "v2", "v3")]
        assert not any(result.done() for result in waiting)
        assert rounds.collected("v5", first.sequence, timeout_seconds=2)
        assert all(result.result(timeout=2) for result in waiting)
    for profile in ("v1", "v2", "v3"):
        rounds.finish(profile, first.sequence)
        assert rounds.enter(profile, tasks) is None
    rounds.finish("v5", first.sequence)
    following = rounds.enter("v1", tasks)
    assert following is not None and following.sequence > first.sequence
    rounds.finish("v1", first.sequence)
    assert rounds.enter("v1", tasks) is None


def test_collection_timeout_does_not_overlap_a_still_running_profile() -> None:
    rounds = RecommendationRounds()
    for profile in ("v1", "v2", "v3"):
        rounds.register(profile)
    tasks = (ScheduledPipelineTask(PipelineTask.FULL_MARKET, datetime(2026, 8, 12, 10, tzinfo=SHANGHAI), MarketPhase.BEFORE_CLOSE),)
    first = rounds.enter("v1", tasks)
    assert first is not None
    rounds.enter("v2", tasks)
    assert not rounds.collected("v1", first.sequence, timeout_seconds=0)
    rounds.finish("v1", first.sequence)
    assert rounds.enter("v1", tasks) is None
    assert not rounds.collected("v2", first.sequence, timeout_seconds=0)
    rounds.finish("v2", first.sequence)
    assert rounds.enter("v1", tasks) is not None


def test_unavailable_profile_does_not_hold_failed_wave_forever() -> None:
    now = [0.0]
    rounds = RecommendationRounds(monotonic=lambda: now[0])
    rounds.register("v1")
    rounds.register("v5")
    tasks = (ScheduledPipelineTask(PipelineTask.FULL_MARKET, datetime(2026, 8, 12, 10, tzinfo=SHANGHAI), MarketPhase.BEFORE_CLOSE),)
    first = rounds.enter("v1", tasks)
    assert first is not None
    rounds.finish("v1", first.sequence)
    assert rounds.enter("v1", tasks) is None
    now[0] = 61.0
    following = rounds.enter("v1", tasks)
    assert following is not None and following.sequence != first.sequence
    rounds.withdraw("v5")
    assert rounds.collected("v1", following.sequence, timeout_seconds=0)
