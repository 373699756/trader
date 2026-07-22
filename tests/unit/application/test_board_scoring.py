from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from trader.application.board_scoring import BoardScoringCoordinator
from trader.application.board_scoring_cache import ScoringCacheContext
from trader.domain.models import Board, BoardStrategyPolicy, Strategy

NOW = datetime(2026, 7, 16, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _policy(board: Board) -> BoardStrategyPolicy:
    return BoardStrategyPolicy(
        policy_id=f"v16:today:{board.value}",
        version="v16",
        board=board,
        strategy=Strategy.TODAY,
        candidate_weights={
            "liquidity": 0.30,
            "intraday_structure": 0.25,
            "turnover_state": 0.20,
            "peer_gap": 0.15,
            "data_completeness": 0.10,
        },
        local_weights={
            "intraday_structure": 0.30,
            "turnover_state": 0.20,
            "peer_gap": 0.20,
            "liquidity_execution": 0.20,
            "stability": 0.10,
        },
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
            Strategy.TODAY,
            features,
            {board: _policy(board) for board in (Board.MAIN, Board.CHINEXT, Board.STAR)},
            ScoringCacheContext("2026-07-16", "today_main", "epoch-1", "data-1", NOW),
            lambda *_args: (_ for _ in ()).throw(AssertionError("quality gate should keep the tiny fixture empty")),
        )
        status = coordinator.status()
    finally:
        coordinator.stop()

    assert {batch.board for batch in batches} == {Board.MAIN, Board.CHINEXT, Board.STAR}
    assert {batch.merge_epoch for batch in batches} == {"epoch-1"}
    assert all(batch.status == "empty" for batch in batches)
    assert all(lane["workers"] == 1 and lane["queue_capacity"] == 1 for lane in status.values())
    assert all(lane["queue_wait_samples"] == 1 for lane in status.values())
    assert all(float(lane["queue_wait_p95_ms"]) >= 0.0 for lane in status.values())
