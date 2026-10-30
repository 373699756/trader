from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

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
    assert not rounds.cancelled(first.sequence)
    rounds.finish("v1", first.sequence)
    following = rounds.enter("v1", _tasks())
    assert following is not None and following.sequence > first.sequence
