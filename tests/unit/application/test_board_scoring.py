from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from threading import Event
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from trader.application.board_scoring import (
    BoardScoringCoordinator,
    BoardScoringPlan,
    _all_candidates_below_reliability,
    _LatestBoardLane,
)
from trader.application.board_scoring_cache import ScoringCacheContext
from trader.domain.market.models import Board, FeatureSnapshot
from trader.domain.recommendation.models import (
    BoardScoreBatch,
    BoardStrategyPolicy,
    FusionMode,
    Recommendation,
    RecommendationAction,
    ScoreBreakdown,
    Strategy,
)
from trader.domain.recommendation.strategies.composition import LocalScoreResult

NOW = datetime(2026, 7, 16, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _policy(board: Board) -> BoardStrategyPolicy:
    return BoardStrategyPolicy(
        policy_id=f"v16:today:{board.value}",
        version="v16",
        board=board,
        strategy=Strategy.TODAY,
        candidate_weights={
            "liquidity": 0.3529411764705882,
            "intraday_structure": 0.2941176470588235,
            "turnover_state": 0.2352941176470588,
            "data_completeness": 0.1176470588235294,
        },
        local_weights={
            "intraday_structure": 0.375,
            "turnover_state": 0.25,
            "liquidity_execution": 0.25,
            "stability": 0.125,
        },
        candidate_min_score=100.0,
    )


def test_coordinator_owns_three_single_worker_lanes_and_preserves_epoch(application_feature_factory) -> None:
    features = tuple(
        replace(
            application_feature_factory(code, NOW),
            quote=replace(application_feature_factory(code, NOW).quote, board=board),
        )
        for code, board in (("600001", Board.MAIN), ("300001", Board.CHINEXT), ("688001", Board.STAR))
    )
    coordinator = BoardScoringCoordinator()
    coordinator.start()
    try:
        batches = coordinator.score(
            BoardScoringPlan(
                Strategy.TODAY,
                features,
                {board: _policy(board) for board in (Board.MAIN, Board.CHINEXT, Board.STAR)},
                ScoringCacheContext("2026-07-16", "today_main", "epoch-1", "data-1", NOW),
            ),
            lambda *_args: (_ for _ in ()).throw(AssertionError("quality gate should keep the tiny fixture empty")),
        )
        status = coordinator.status()
    finally:
        coordinator.stop()

    assert {batch.board for batch in batches} == {Board.MAIN, Board.CHINEXT, Board.STAR}
    assert {batch.merge_epoch for batch in batches} == {"epoch-1"}
    assert all(batch.status == "empty" for batch in batches)
    assert all(lane["workers"] == 1 and lane["queue_capacity"] == 3 for lane in status.values())
    assert all(lane["queue_wait_samples"] == 1 for lane in status.values())
    assert all(float(lane["queue_wait_p95_ms"]) >= 0.0 for lane in status.values())


def test_board_lane_keeps_pending_strategies_isolated_and_prioritizes_tomorrow() -> None:
    lane = _LatestBoardLane("board-lane-regression")
    active_started = Event()
    release_active = Event()
    execution_order: list[Strategy] = []

    def batch(strategy: Strategy) -> BoardScoreBatch:
        execution_order.append(strategy)
        return BoardScoreBatch(
            Board.MAIN,
            strategy,
            "epoch-1",
            f"policy-{strategy.value}",
            "empty",
            (),
            (),
            "v16",
        )

    def active() -> BoardScoreBatch:
        active_started.set()
        assert release_active.wait(timeout=2.0)
        return batch(Strategy.TODAY)

    lane.start()
    try:
        today = lane.submit(Strategy.TODAY, active)
        assert active_started.wait(timeout=2.0)
        d25 = lane.submit(Strategy.D25, lambda: batch(Strategy.D25))
        tomorrow = lane.submit(Strategy.TOMORROW, lambda: batch(Strategy.TOMORROW))
        release_active.set()

        assert today.result(timeout=2.0).strategy is Strategy.TODAY
        assert tomorrow.result(timeout=2.0).strategy is Strategy.TOMORROW
        assert d25.result(timeout=2.0).strategy is Strategy.D25
        status = lane.status()
    finally:
        release_active.set()
        lane.stop()

    assert execution_order == [Strategy.TODAY, Strategy.TOMORROW, Strategy.D25]
    assert status["superseded_count"] == 0
    assert status["pending"] == 0


def test_board_lane_supersedes_only_an_older_pending_epoch_of_the_same_strategy() -> None:
    lane = _LatestBoardLane("board-lane-latest-wins")
    active_started = Event()
    release_active = Event()

    def batch(strategy: Strategy, epoch: str) -> BoardScoreBatch:
        return BoardScoreBatch(
            Board.MAIN,
            strategy,
            epoch,
            f"policy-{strategy.value}",
            "empty",
            (),
            (),
            "v16",
        )

    def active() -> BoardScoreBatch:
        active_started.set()
        assert release_active.wait(timeout=2.0)
        return batch(Strategy.TODAY, "active")

    lane.start()
    try:
        today = lane.submit(Strategy.TODAY, active)
        assert active_started.wait(timeout=2.0)
        stale = lane.submit(Strategy.TOMORROW, lambda: batch(Strategy.TOMORROW, "stale"))
        latest = lane.submit(Strategy.TOMORROW, lambda: batch(Strategy.TOMORROW, "latest"))
        release_active.set()

        with pytest.raises(RuntimeError, match="superseded"):
            stale.result(timeout=2.0)
        assert today.result(timeout=2.0).merge_epoch == "active"
        assert latest.result(timeout=2.0).merge_epoch == "latest"
        status = lane.status()
    finally:
        release_active.set()
        lane.stop()

    assert status["superseded_count"] == 1


def test_board_lane_priority_cycle_does_not_starve_other_strategies() -> None:
    lane = _LatestBoardLane("board-lane-fairness")
    today_started = Event()
    release_today = Event()
    tomorrow_started = Event()
    release_tomorrow = Event()
    execution_order: list[str] = []

    def batch(strategy: Strategy, epoch: str) -> BoardScoreBatch:
        execution_order.append(epoch)
        return BoardScoreBatch(
            Board.MAIN,
            strategy,
            epoch,
            f"policy-{strategy.value}",
            "empty",
            (),
            (),
            "v16",
        )

    def active_today() -> BoardScoreBatch:
        today_started.set()
        assert release_today.wait(timeout=2.0)
        return batch(Strategy.TODAY, "today")

    def first_tomorrow() -> BoardScoreBatch:
        tomorrow_started.set()
        assert release_tomorrow.wait(timeout=2.0)
        return batch(Strategy.TOMORROW, "tomorrow-1")

    lane.start()
    try:
        today = lane.submit(Strategy.TODAY, active_today)
        assert today_started.wait(timeout=2.0)
        d25 = lane.submit(Strategy.D25, lambda: batch(Strategy.D25, "d25"))
        tomorrow = lane.submit(Strategy.TOMORROW, first_tomorrow)
        release_today.set()
        assert tomorrow_started.wait(timeout=2.0)
        next_tomorrow = lane.submit(
            Strategy.TOMORROW,
            lambda: batch(Strategy.TOMORROW, "tomorrow-2"),
        )
        release_tomorrow.set()

        assert today.result(timeout=2.0).merge_epoch == "today"
        assert tomorrow.result(timeout=2.0).merge_epoch == "tomorrow-1"
        assert d25.result(timeout=2.0).merge_epoch == "d25"
        assert next_tomorrow.result(timeout=2.0).merge_epoch == "tomorrow-2"
    finally:
        release_today.set()
        release_tomorrow.set()
        lane.stop()

    assert execution_order == ["today", "tomorrow-1", "d25", "tomorrow-2"]


def test_board_reliability_degrades_only_when_every_ranked_candidate_is_below_threshold() -> None:
    mixed = (
        SimpleNamespace(features=SimpleNamespace(board_data_reliability=0.84)),
        SimpleNamespace(features=SimpleNamespace(board_data_reliability=0.90)),
    )
    unreliable = (
        SimpleNamespace(features=SimpleNamespace(board_data_reliability=0.84)),
        SimpleNamespace(features=SimpleNamespace(board_data_reliability=0.80)),
    )

    assert _all_candidates_below_reliability(mixed, 0.85) is False
    assert _all_candidates_below_reliability(unreliable, 0.85) is True


def test_coordinator_builds_population_context_but_scores_only_fresh_candidates(
    application_feature_factory,
) -> None:
    population = tuple(
        replace(
            application_feature_factory(f"600{index:03d}", NOW),
            quote=replace(
                application_feature_factory(f"600{index:03d}", NOW).quote,
                board=Board.MAIN,
                data_version="population-v1",
            ),
        )
        for index in range(60)
    )
    candidate = replace(
        population[7],
        quote=replace(population[7].quote, price=99.0, data_version="candidate-v2"),
    )
    policies = {
        board: replace(_policy(board), candidate_min_score=0.0) for board in (Board.MAIN, Board.CHINEXT, Board.STAR)
    }
    scored_codes: list[str] = []

    def score_one(
        strategy: Strategy,
        feature: FeatureSnapshot,
        _policy_value: BoardStrategyPolicy,
        local: LocalScoreResult,
    ) -> Recommendation:
        scored_codes.append(feature.quote.code)
        score = ScoreBreakdown(
            components=local.components,
            base_score=local.base_score,
            local_risk_penalty=0.0,
            local_score=local.base_score,
            deepseek_score=None,
            confidence_coverage=0.0,
            deepseek_risk_penalty=0.0,
            final_score=local.base_score,
            fusion_mode=FusionMode.LOCAL_DEGRADED,
            fusion_applied=False,
        )
        return Recommendation(
            strategy,
            feature,
            score,
            (),
            (),
            None,
            RecommendationAction.OBSERVE,
            "test",
            False,
        )

    batches = BoardScoringCoordinator().score(
        BoardScoringPlan(
            Strategy.TODAY,
            (candidate,),
            policies,
            ScoringCacheContext("2026-07-16", "today_main", "population-epoch", "population-v1", NOW),
            population,
        ),
        score_one,
    )

    main = next(batch for batch in batches if batch.board is Board.MAIN)
    assert scored_codes == [candidate.quote.code]
    assert tuple(item.features.quote.code for item in main.recommendations) == (candidate.quote.code,)
    assert main.recommendations[0].features.quote.price == 99.0
    assert main.recommendations[0].features.quote.data_version == "candidate-v2"
