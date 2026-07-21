"""Three isolated latest-wins single-worker board scoring lanes."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import replace

from trader.application.board_scoring_cache import BoardScoringCache, ScoringCacheContext
from trader.application.workers import BoundedExecutor
from trader.domain.board_scoring import (
    BoardCrossSection,
    apply_board_policy,
    board_candidate_score,
    build_board_cross_section,
    candidate_fields,
    score_board_strategy,
)
from trader.domain.models import (
    Board,
    BoardScoreBatch,
    BoardStrategyPolicy,
    FeatureSnapshot,
    Recommendation,
    Strategy,
)
from trader.domain.strategies.composition import LocalScoreResult

ScoreOne = Callable[[Strategy, FeatureSnapshot, BoardStrategyPolicy, LocalScoreResult], Recommendation]


class _LatestBoardLane:
    """One active task plus one replaceable pending task."""

    def __init__(self, name: str) -> None:
        self._executor = BoundedExecutor(worker_count=1, queue_capacity=0, thread_name_prefix=name)
        self._lock = threading.Lock()
        self._accepting = False
        self._pending: tuple[Callable[[], BoardScoreBatch], Future[BoardScoreBatch]] | None = None
        self._superseded_count = 0

    def start(self) -> None:
        self._executor.start()
        with self._lock:
            self._accepting = True

    def submit(self, operation: Callable[[], BoardScoreBatch]) -> Future[BoardScoreBatch]:
        submitted: Future[BoardScoreBatch] | None = None
        with self._lock:
            if not self._accepting:
                rejected: Future[BoardScoreBatch] = Future()
                rejected.set_exception(RuntimeError("board scoring lane is stopped"))
                return rejected
            submitted = self._executor.submit(operation)
            if submitted is None:
                proxy: Future[BoardScoreBatch] = Future()
                previous = self._pending
                self._pending = (operation, proxy)
                if previous is not None and not previous[1].done():
                    self._superseded_count += 1
                    previous[1].set_exception(RuntimeError("board scoring request superseded by a newer epoch"))
                return proxy
        submitted.add_done_callback(self._drain_pending)
        return submitted

    def stop(self) -> None:
        with self._lock:
            self._accepting = False
            pending = self._pending
            self._pending = None
        if pending is not None and not pending[1].done():
            pending[1].set_exception(RuntimeError("board scoring lane stopped before pending task started"))
        self._executor.stop(wait=True, cancel_futures=True)

    def status(self) -> Mapping[str, int | bool]:
        status = self._executor.status()
        with self._lock:
            return {
                **{name: value for name, value in status.items() if isinstance(value, (int, bool))},
                "queue_capacity": 1,
                "pending": self._pending is not None,
                "superseded_count": self._superseded_count,
            }

    def _drain_pending(self, _completed: Future[BoardScoreBatch]) -> None:
        submitted: Future[BoardScoreBatch] | None = None
        proxy: Future[BoardScoreBatch] | None = None
        with self._lock:
            if not self._accepting or self._pending is None:
                return
            operation, proxy = self._pending
            self._pending = None
            submitted = self._executor.submit(operation)
            if submitted is None:
                if not proxy.done():
                    proxy.set_exception(RuntimeError("board scoring lane could not start pending task"))
                return
        submitted.add_done_callback(self._drain_pending)
        submitted.add_done_callback(lambda future: _copy_future(future, proxy))


class BoardScoringCoordinator:
    def __init__(self, cache: BoardScoringCache | None = None) -> None:
        self._lanes = {
            Board.MAIN: _LatestBoardLane("main-score"),
            Board.CHINEXT: _LatestBoardLane("chinext-score"),
            Board.STAR: _LatestBoardLane("star-score"),
        }
        self._cache = cache
        self._lock = threading.Lock()
        self._running = False

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            started: list[_LatestBoardLane] = []
            try:
                for lane in self._lanes.values():
                    lane.start()
                    started.append(lane)
            except BaseException:
                for lane in reversed(started):
                    lane.stop()
                raise
            self._running = True

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
        for lane in self._lanes.values():
            lane.stop()

    def enrich(
        self,
        strategy: Strategy,
        features: Sequence[FeatureSnapshot],
        policy: BoardStrategyPolicy,
        context: ScoringCacheContext,
    ) -> tuple[FeatureSnapshot, ...]:
        cross_section = self._cross_section(policy.board, features, context)
        return apply_board_policy(cross_section, strategy, policy)

    def preselect(
        self,
        strategy: Strategy,
        features: Sequence[FeatureSnapshot],
        policy: BoardStrategyPolicy,
        context: ScoringCacheContext,
        *,
        limit: int = 120,
    ) -> tuple[FeatureSnapshot, ...]:
        if limit < 0 or limit > 120:
            raise ValueError("board candidate limit must be between 0 and 120")
        cross_section = self._cross_section(policy.board, features, context)
        enriched = apply_board_policy(cross_section, strategy, policy)
        loader = lambda: _select_candidates(enriched, strategy, policy, limit=120)
        if self._cache is None:
            return loader()[:limit]
        return self._cache.candidate_batch(policy, context, enriched, loader)[:limit]

    def score(
        self,
        strategy: Strategy,
        features: Sequence[FeatureSnapshot],
        policies: Mapping[Board, BoardStrategyPolicy],
        context: ScoringCacheContext,
        score_one: ScoreOne,
    ) -> tuple[BoardScoreBatch, ...]:
        if set(policies) != set(self._lanes):
            raise ValueError("board scoring requires exactly three board policies")
        grouped = {board: tuple(item for item in features if item.quote.board is board) for board in self._lanes}
        with self._lock:
            running = self._running
        if not running:
            return tuple(
                self._score_board(strategy, board, grouped[board], policies[board], context, score_one)
                for board in self._lanes
            )

        futures: dict[Board, Future[BoardScoreBatch]] = {}
        for board, lane in self._lanes.items():
            def score_lane(board: Board = board) -> BoardScoreBatch:
                return self._score_board(
                    strategy,
                    board,
                    grouped[board],
                    policies[board],
                    context,
                    score_one,
                )

            futures[board] = lane.submit(score_lane)
        batches: list[BoardScoreBatch] = []
        for board in self._lanes:
            try:
                batches.append(futures[board].result())
            except Exception as exc:
                policy = policies[board]
                batches.append(
                    BoardScoreBatch(
                        board,
                        strategy,
                        context.merge_epoch,
                        policy.policy_id,
                        "failed",
                        (),
                        (type(exc).__name__,),
                        policy.version,
                    )
                )
        return tuple(batches)

    def status(self) -> Mapping[str, Mapping[str, int | bool]]:
        return {board.value: lane.status() for board, lane in self._lanes.items()}

    def _score_board(
        self,
        strategy: Strategy,
        board: Board,
        features: Sequence[FeatureSnapshot],
        policy: BoardStrategyPolicy,
        context: ScoringCacheContext,
        score_one: ScoreOne,
    ) -> BoardScoreBatch:
        if not features:
            return BoardScoreBatch(
                board,
                strategy,
                context.merge_epoch,
                policy.policy_id,
                "empty",
                (),
                (),
                policy.version,
            )
        try:
            cross_section = self._cross_section(board, features, context)
            enriched = apply_board_policy(cross_section, strategy, policy)
            selected = (
                self._cache.candidate_batch(
                    policy,
                    context,
                    enriched,
                    lambda: _select_candidates(enriched, strategy, policy, limit=120),
                )
                if self._cache is not None
                else _select_candidates(enriched, strategy, policy, limit=120)
            )
            if not selected:
                return BoardScoreBatch(
                    board,
                    strategy,
                    context.merge_epoch,
                    policy.policy_id,
                    "empty",
                    (),
                    (),
                    policy.version,
                    cross_section.population.population_version,
                )
            recommendations: list[Recommendation] = []
            for item in selected:
                def load_local_score(
                    item: FeatureSnapshot = item,
                    policy: BoardStrategyPolicy = policy,
                ) -> LocalScoreResult:
                    return score_board_strategy(item, policy)

                local_score = (
                    self._cache.local_score(policy, context, item, load_local_score)
                    if self._cache is not None
                    else score_board_strategy(item, policy)
                )
                recommendations.append(score_one(strategy, item, policy, local_score))
            ordered = sorted(
                recommendations,
                key=lambda item: (-item.score.local_score, item.features.quote.code),
            )
            ranked = tuple(replace(item, board_rank=index) for index, item in enumerate(ordered, start=1))
            reasons: list[str] = []
            if cross_section.population.status != "current":
                reasons.append(f"board_population_{cross_section.population.status}")
            if any(item.features.board_data_reliability < policy.minimum_reliability for item in ranked):
                reasons.append("board_data_reliability_below_threshold")
            return BoardScoreBatch(
                board,
                strategy,
                context.merge_epoch,
                policy.policy_id,
                "degraded" if reasons else "success",
                ranked,
                tuple(reasons),
                policy.version,
                cross_section.population.population_version,
            )
        except Exception as exc:
            return BoardScoreBatch(
                board,
                strategy,
                context.merge_epoch,
                policy.policy_id,
                "failed",
                (),
                (type(exc).__name__,),
                policy.version,
            )

    def _cross_section(
        self,
        board: Board,
        features: Sequence[FeatureSnapshot],
        context: ScoringCacheContext,
    ) -> BoardCrossSection:
        if self._cache is not None:
            return self._cache.cross_section(board, features, context)
        return build_board_cross_section(
            features,
            board=board,
            merge_epoch=context.merge_epoch,
            trade_date=context.trade_date,
            phase=context.phase,
            data_version=context.data_version,
        )


def _select_candidates(
    features: Sequence[FeatureSnapshot],
    strategy: Strategy,
    policy: BoardStrategyPolicy,
    *,
    limit: int,
) -> tuple[FeatureSnapshot, ...]:
    scored: list[tuple[bool, float, FeatureSnapshot]] = []
    required_fields = candidate_fields(strategy)
    for item in features:
        if item.missing_ratio(required_fields) > 0.30:
            continue
        score = board_candidate_score(item, policy)
        if score < policy.candidate_min_score:
            continue
        scored.append(
            (
                item.board_data_reliability < policy.minimum_reliability,
                score,
                replace(item, values={**dict(item.values), "board_candidate_score": score}),
            )
        )
    scored.sort(key=lambda row: (row[0], -row[1], row[2].quote.code))
    return tuple(row[2] for row in scored[:limit])


def _copy_future(source: Future[BoardScoreBatch], target: Future[BoardScoreBatch]) -> None:
    if target.done():
        return
    if source.cancelled():
        target.cancel()
        return
    error = source.exception()
    if error is not None:
        target.set_exception(error)
    else:
        target.set_result(source.result())


__all__ = ["BoardScoringCoordinator", "ScoreOne"]
