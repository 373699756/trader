from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from itertools import combinations
import gc
import weakref

import pytest

from trader.recommendation.application.runtime.cadence import CadencePlanner, CadencePolicy, PipelineTask, ScheduledPipelineTask
from trader.recommendation.application.runtime.recommendation_round import RecommendationRounds
from trader.recommendation.application.runtime.schedule import SHANGHAI, MarketPhase
from trader.recommendation.application.runtime.trading_session import TradingSession


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
    observations = []
    for _ in range(100):
        before = now[0]
        admission = rounds.enter("v1", _tasks())
        assert admission.started_monotonic == before + 10.0
        assert rounds.enter("v5", _tasks()) is admission
        context = rounds.inputs("v1", admission.sequence)
        retained.append(weakref.ref(context.inputs))
        observations.append(weakref.ref(rounds._observation))
        rounds.finish("v1", admission.sequence)
        rounds.finish("v5", admission.sequence)
        del context
    gc.collect()
    assert all(reference() is None for reference in retained)
    assert all(reference() is None for reference in observations[:-1])
    assert rounds.status().database.connection_attempts == rounds.status().database.statements == 0


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


def test_failed_publication_preparation_preserves_previously_deferred_tasks():
    fail_preparation = [False]

    def prepare():
        if fail_preparation[0]:
            raise RuntimeError("publication_sequence_unavailable")

    rounds = RecommendationRounds()
    rounds.register("v1", prepare)
    market = _tasks()[0]
    auxiliary = replace(market, task=PipelineTask.INTRADAY_TAIL)
    first = rounds.enter("v1", (auxiliary,))
    assert first is not None
    assert rounds.enter("v1", (market,)) is None
    rounds.finish("v1", first.sequence)
    fail_preparation[0] = True
    with pytest.raises(RuntimeError, match="publication_sequence_unavailable"):
        rounds.enter("v1", (auxiliary,))
    fail_preparation[0] = False
    following = rounds.enter("v1", (auxiliary,))
    assert following is not None
    try:
        assert {task.task for task in following.tasks} == {PipelineTask.FULL_MARKET, PipelineTask.INTRADAY_TAIL}
    finally:
        rounds.finish("v1", following.sequence)


def test_unqueued_release_preserves_the_latest_tasks_and_requires_the_running_profile_admission():
    rounds = RecommendationRounds()
    rounds.register("v1")
    market = _tasks()[0]
    auxiliary = replace(market, task=PipelineTask.INTRADAY_TAIL)
    first = rounds.enter("v1", (auxiliary,))
    assert first is not None
    latest = replace(market, scheduled_at=market.scheduled_at + timedelta(seconds=10))
    assert rounds.enter("v1", (latest,)) is None
    rounds.release_unqueued("v1", first.sequence)
    following = rounds.enter("v1", (auxiliary,))
    assert following is not None
    assert tuple(task for task in following.tasks if task.task is PipelineTask.FULL_MARKET) == (latest,)
    rounds.release_unqueued("unregistered", following.sequence)
    rounds.release_unqueued("v1", first.sequence)
    rounds.finish("v1", following.sequence)
    final = rounds.enter("v1", (auxiliary,))
    assert final is not None
    try:
        assert final.tasks == (auxiliary,)
    finally:
        rounds.finish("v1", final.sequence)


def test_auxiliary_cadence_cannot_starve_market_due_during_a_busy_round():
    at = datetime(2026, 8, 12, 12, 59, 59, 300000, tzinfo=SHANGHAI)
    now = [0.0]
    planner = CadencePlanner(CadencePolicy.from_seconds({
        "full_market": {"before_close": 10},
        "candidate_quotes": {"before_close": 10},
        "topk_quotes": {"before_close": 10},
        "long_quotes": {"before_close": 10},
        "score": {"before_close": 10},
        "intraday_tail": {"afternoon": 10},
        "industry_heat": {"afternoon": 60},
    }))
    # Main inputs retain their pre-13:00 phase, while minute acquisition starts
    # 0.7 seconds earlier and wins the next admission after a slow round.
    planner.plan(at, is_trading_day=True)
    rounds = RecommendationRounds(monotonic=lambda: now[0])
    profiles = ("v1", "v2", "v3", "v5")
    for profile in profiles:
        rounds.register(profile)
    first_at = at + timedelta(seconds=0.7)
    initial_tasks = planner.plan(first_at, is_trading_day=True).tasks
    assert {item.task for item in initial_tasks} == {PipelineTask.INTRADAY_TAIL, PipelineTask.INDUSTRY_HEAT}
    first = rounds.enter(profiles[0], initial_tasks)
    assert first is not None
    assert all(rounds.enter(profile, initial_tasks) is first for profile in profiles[1:])
    for seconds in (10.0, 10.7, 20.0):
        now[0] = float(seconds)
        due = planner.plan(at + timedelta(seconds=seconds), is_trading_day=True)
        main = tuple(task for task in due.tasks if task.task in {PipelineTask.FULL_MARKET, PipelineTask.CANDIDATE_QUOTES})
        assert len(main) == (0 if seconds == 10.7 else 2)
        tasks = tuple(task for task in due.tasks if task.task is not PipelineTask.LONG_QUOTES)
        assert all(rounds.enter(profile, tasks) is None for profile in profiles)
        assert first.tasks == initial_tasks
    for profile in profiles:
        rounds.finish(profile, first.sequence)
    now[0] = 20.7
    following_at = at + timedelta(seconds=20.7)
    following_tasks = planner.plan(following_at, is_trading_day=True).tasks
    assert {item.task for item in following_tasks} == {PipelineTask.INTRADAY_TAIL}
    following = rounds.enter(profiles[0], following_tasks)
    assert following is not None
    assert {item.task for item in following.tasks} == {
        PipelineTask.FULL_MARKET, PipelineTask.CANDIDATE_QUOTES, PipelineTask.TOPK_QUOTES, PipelineTask.INTRADAY_TAIL,
    }
    assert all(rounds.enter(profile, following_tasks) is following for profile in profiles[1:])
    assert following.deadline_monotonic - following.started_monotonic == pytest.approx(120.0)


@pytest.mark.parametrize("elapsed", (19.0, 121.0))
def test_busy_or_late_joining_profile_keeps_only_latest_uncovered_task(elapsed):
    now = [0.0]
    rounds = RecommendationRounds(monotonic=lambda: now[0])
    for profile in ("v1", "v5"):
        rounds.register(profile)
    auxiliary = replace(_tasks()[0], task=PipelineTask.INTRADAY_TAIL)
    first = rounds.enter("v1", (auxiliary,))
    newer = replace(_tasks()[0], scheduled_at=auxiliary.scheduled_at + timedelta(seconds=1))
    latest = replace(newer, scheduled_at=newer.scheduled_at + timedelta(seconds=10))
    assert rounds.enter("v5", (newer,)) is first
    assert rounds.enter("v1", (latest,)) is None
    assert rounds.enter("v5", (newer,)) is None
    assert first.tasks == (auxiliary,)
    now[0] = elapsed
    for profile in ("v1", "v5"):
        rounds.finish(profile, first.sequence)
    following = rounds.enter("v1", (replace(auxiliary, scheduled_at=latest.scheduled_at),))
    assert following is not None
    assert tuple(item for item in following.tasks if item.task is PipelineTask.FULL_MARKET) == (latest,)
    assert rounds.enter("v5", ()) is following
    for profile in ("v1", "v5"):
        rounds.finish(profile, following.sequence)
    next_round = rounds.enter("v1", (auxiliary,))
    assert next_round.tasks == (auxiliary,)


@pytest.mark.parametrize("boundary", ("date", "phase", "generation"))
def test_uncovered_tasks_do_not_cross_the_next_session_or_phase(boundary):
    now = [0.0]
    wall = [_tasks()[0].scheduled_at]
    session = TradingSession(lambda: wall[0], lambda: now[0])
    lease = session.capture() if boundary == "generation" else None
    auxiliary = replace(_tasks()[0], task=PipelineTask.INTRADAY_TAIL, session=lease)
    market = replace(_tasks()[0], session=lease)
    rounds = RecommendationRounds(monotonic=lambda: now[0])
    rounds.register("v1")
    first = rounds.enter("v1", (auxiliary,))
    assert rounds.enter("v1", (market,)) is None
    rounds.finish("v1", first.sequence)
    if boundary == "date":
        wall[0] += timedelta(days=1)
    elif boundary == "phase":
        wall[0] = wall[0].replace(hour=15)
    else:
        wall[0] -= timedelta(seconds=1)
    current = replace(
        auxiliary,
        scheduled_at=wall[0],
        phase=MarketPhase.AFTER_CLOSE if boundary == "phase" else auxiliary.phase,
        session=session.capture() if boundary == "generation" else None,
    )
    following = rounds.enter("v1", (current,))
    assert following.tasks == (current,)


def test_already_covered_tasks_do_not_create_catchup_and_last_withdraw_releases_pending():
    rounds = RecommendationRounds()
    rounds.register("v1")
    market = _tasks()[0]
    first = rounds.enter("v1", (market,))
    assert rounds.enter("v1", (replace(market, scheduled_at=market.scheduled_at + timedelta(seconds=10)),)) is None
    rounds.finish("v1", first.sequence)
    auxiliary = replace(market, task=PipelineTask.INTRADAY_TAIL)
    following = rounds.enter("v1", (auxiliary,))
    assert following.tasks == (auxiliary,)
    queued = replace(market, scheduled_at=market.scheduled_at + timedelta(seconds=20))
    retained = weakref.ref(queued)
    assert rounds.enter("v1", (queued,)) is None
    del queued
    gc.collect()
    assert retained() is not None
    rounds.withdraw("v1")
    rounds.finish("v1", following.sequence)
    gc.collect()
    assert retained() is None


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


def test_status_keeps_timeout_and_cleanup_separate_and_read_does_not_expire_work():
    now = [0.0]
    rounds = RecommendationRounds(monotonic=lambda: now[0])
    rounds.register("v1")
    rounds.register("v5")
    admission = rounds.enter("v1", _tasks())
    rounds.enter("v5", _tasks())
    now[0] = 120.0
    assert rounds.status().outcome == "running"
    assert rounds.cancelled(admission.sequence)
    rounds.finish("v1", admission.sequence)
    assert rounds.status().running_profiles == ("v5",)
    now[0] = 122.0
    rounds.finish("v5", admission.sequence)
    status = rounds.status()
    assert status.finished and status.outcome == "timeout"
    assert status.elapsed_ms == 122000 and status.cleanup_ms == 2000
    assert not status.running_profiles and not status.pending_profiles
    now[0] = 140
    assert rounds.status() == status
    following = rounds.enter("v5", _tasks())
    assert following.sequence == admission.sequence + 1
    assert rounds.status().outcome == "running"
    assert rounds.status().cleanup_ms == 0


def test_cancelled_round_waits_for_descendant_worker_and_counts_its_late_sql():
    from threading import Event
    from trader.infra.sqlite_connection import SqliteSession
    from trader.infra.workers import BoundedExecutor, SubmitOrReject

    rounds = RecommendationRounds()
    rounds.register("v1")
    admission = rounds.enter("v1", _tasks())
    release = Event()
    workers = BoundedExecutor(worker_count=1, queue_capacity=1, thread_name_prefix="round-child")
    workers.start()

    def child():
        assert release.wait(3)
        with SqliteSession(":memory:") as connection:
            connection.run("SELECT 1").close()

    try:
        with rounds.observe(admission.sequence):
            future = SubmitOrReject(workers, child)
        rounds.finish("v1", admission.sequence)
        assert not rounds.status().finished
        assert rounds.status().database.worker_inflight == 1
        assert rounds.enter("v1", _tasks()) is None
        release.set()
        future.result(timeout=3)
        assert workers.stop().completed
        with rounds._condition:
            assert rounds._condition.wait_for(lambda: rounds.status().finished, timeout=2)
        status = rounds.status()
        assert status.database.worker_inflight == 0
        assert status.database.connection_attempts == 1
        assert status.database.statements == 2
        assert rounds.enter("v1", _tasks()).sequence == admission.sequence + 1
    finally:
        release.set()
        workers.stop()
