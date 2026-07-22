from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trader.application.board_scoring import BoardScoringCoordinator
from trader.application.board_scoring_cache import ScoringCacheContext
from trader.bootstrap import _recommendation_policy
from trader.domain.board_scoring import board_candidate_score, enrich_board_features, score_board_strategy
from trader.domain.models import (
    Board,
    FeatureSnapshot,
    FusionMode,
    MarketQuote,
    Recommendation,
    RecommendationAction,
    ScoreBreakdown,
    Strategy,
)
from trader.domain.ranking import select_top_k
from trader.domain.strategies.composition import LocalScoreResult
from trader.infrastructure.settings import load_runtime_settings, load_strategy_settings

NOW = datetime(2026, 7, 16, 14, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def _feature(code: str, board: Board, index: int) -> FeatureSnapshot:
    quote = MarketQuote(
        code=code,
        name=f"fixture-{code}",
        price=12.0 + index / 100.0,
        previous_close=12.0,
        open_price=12.0,
        high=12.5,
        low=11.8,
        pct_change=2.0,
        change_5m=1.0,
        speed=1.0,
        volume_ratio=2.0,
        turnover_rate=2.5,
        amount=300_000_000.0 + index * 1_000_000.0,
        amplitude=4.0,
        market_cap=30_000_000_000.0,
        industry=f"industry-{index % 10}",
        source="fixture",
        source_time=NOW,
        received_time=NOW,
        data_version="v16-fixture",
        board=board,
    )
    values = {
        "amount_median_20d": 200_000_000.0 + index * 1_000_000.0,
        "turnover_median_20d": 1.5,
        "return_3d": 3.0,
        "return_5d": 5.0,
        "return_10d": 7.0,
        "return_20d": 10.0,
        "return_60d": 15.0,
        "volatility_20d": 2.0,
        "max_drawdown_20d": -8.0,
        "trend_score": 100.0,
        "ma20_60_position": 70.0,
        "ma20_60_structure": 70.0,
        "ma_slope": 70.0,
        "breakout_20d": 70.0,
        "industry_trend": 70.0,
        "tail_return_30m": 70.0,
        "tail_volume_ratio": 70.0,
        "close_location": 70.0,
        "capacity_score": 100.0,
        "moderate_amplitude": 100.0,
        "price_executability": 100.0,
        "limit_distance_safety": 70.0,
        "quality_score": 70.0,
        "value_score": 70.0,
        "growth_score": 70.0,
        "return_20d_not_overheated": 70.0,
    }
    return FeatureSnapshot(quote, values, NOW, 60, merge_epoch="fixture-epoch")


def _population() -> dict[Board, tuple[FeatureSnapshot, ...]]:
    prefixes = {Board.MAIN: "600", Board.CHINEXT: "300", Board.STAR: "688"}
    return {
        board: tuple(_feature(f"{prefix}{index:03d}", board, index) for index in range(120))
        for board, prefix in prefixes.items()
    }


def _nearest_rank(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(probability * len(ordered)) - 1)]


def _summary(values: list[float], budget: float) -> dict[str, float | int | bool]:
    return {
        "samples": len(values),
        "p50_ms": round(_nearest_rank(values, 0.50), 3),
        "p95_ms": round(_nearest_rank(values, 0.95), 3),
        "maximum_ms": round(max(values), 3),
        "budget_ms": budget,
        "passed": _nearest_rank(values, 0.95) <= budget,
    }


def _distribution(values: list[float]) -> dict[str, float | int]:
    return {
        "samples": len(values),
        "p50_ms": round(_nearest_rank(values, 0.50), 3),
        "p95_ms": round(_nearest_rank(values, 0.95), 3),
        "maximum_ms": round(max(values), 3),
    }


def _recommendation(
    strategy: Strategy,
    feature: FeatureSnapshot,
    _board_policy: object,
    local: LocalScoreResult,
) -> Recommendation:
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
        "performance_fixture",
        False,
    )


def _measure(runtime_path: Path, fixture_path: Path) -> dict[str, Any]:
    process_cpu_started = time.process_time()
    runtime = load_runtime_settings(runtime_path)
    strategy = load_strategy_settings(runtime.strategy_config_path)
    policy = _recommendation_policy(strategy)
    manifest = json.loads((fixture_path / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("candidate_rows") != 360:
        raise ValueError("v16 fixture must contain the fixed 360-candidate workload")
    rounds = runtime.performance_budgets.rounds
    if (rounds.warmup, rounds.measurement) != (1, 5):
        raise ValueError("v16 performance rounds must remain warmup=1 and measurement=5")
    populations = _population()
    samples = {
        "board_preselection": [],
        "board_local_scoring": [],
        "three_board_wall_clock": [],
        "global_selection": [],
    }
    sequential_samples: list[float] = []
    speedup_samples: list[float] = []
    peak_items = 0
    coordinator = BoardScoringCoordinator()
    coordinator.start()
    try:
        for round_index in range(rounds.warmup + rounds.measurement):
            preselection_ms = 0.0
            local_ms = 0.0
            sequential_started = time.perf_counter()
            for active_strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25):
                for board, features in populations.items():
                    board_policy = policy.board_policy(active_strategy, board)
                    if board_policy is None:
                        raise ValueError("v16 policy is incomplete")
                    started = time.perf_counter()
                    enriched = enrich_board_features(
                        active_strategy,
                        features,
                        board_policy,
                        merge_epoch="fixture-epoch",
                        trade_date="2026-07-16",
                        phase="afternoon",
                        data_version="fixture-v16",
                    )
                    selected = tuple(
                        item
                        for item in enriched
                        if board_candidate_score(item, board_policy) >= board_policy.candidate_min_score
                    )[:120]
                    preselection_ms = max(preselection_ms, (time.perf_counter() - started) * 1000.0)
                    started = time.perf_counter()
                    for item in selected:
                        _recommendation(
                            active_strategy,
                            item,
                            board_policy,
                            score_board_strategy(item, board_policy),
                        )
                    local_ms = max(local_ms, (time.perf_counter() - started) * 1000.0)
                    peak_items = max(peak_items, len(selected))
            sequential_ms = (time.perf_counter() - sequential_started) * 1000.0

            lane_candidates: dict[Strategy, tuple[Recommendation, ...]] = {}
            lane_started = time.perf_counter()
            all_features = tuple(item for features in populations.values() for item in features)
            for active_strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25):
                policies = {
                    board: board_policy
                    for board in populations
                    if (board_policy := policy.board_policy(active_strategy, board)) is not None
                }
                batches = coordinator.score(
                    active_strategy,
                    all_features,
                    policies,
                    ScoringCacheContext(
                        "2026-07-16",
                        "afternoon",
                        f"fixture-epoch-{round_index}",
                        "fixture-v16",
                        NOW,
                    ),
                    _recommendation,
                )
                if any(batch.status == "failed" for batch in batches):
                    raise ValueError("v16 lane scoring failed")
                lane_candidates[active_strategy] = tuple(
                    recommendation for batch in batches for recommendation in batch.recommendations
                )
                peak_items = max(peak_items, sum(len(items) for items in lane_candidates.values()))
                if len(lane_candidates[active_strategy]) != 360:
                    raise ValueError(
                        "v16 global selection fixture must retain 360 candidates per strategy; "
                        f"got {len(lane_candidates[active_strategy])} for {active_strategy.value}"
                    )
            lane_ms = (time.perf_counter() - lane_started) * 1000.0

            selection_ms = 0.0
            for candidates in lane_candidates.values():
                select_started = time.perf_counter()
                select_top_k(candidates, top_k=10, maximum_per_industry=3, maximum_board_fraction=0.6)
                selection_ms = max(selection_ms, (time.perf_counter() - select_started) * 1000.0)
            if round_index >= rounds.warmup:
                samples["board_preselection"].append(preselection_ms)
                samples["board_local_scoring"].append(local_ms)
                samples["three_board_wall_clock"].append(lane_ms)
                samples["global_selection"].append(selection_ms)
                sequential_samples.append(sequential_ms)
                speedup_samples.append(sequential_ms / lane_ms if lane_ms > 0.0 else 0.0)
    finally:
        lane_status = coordinator.status()
        coordinator.stop()
    budgets = runtime.performance_budgets.latency_p95_ms
    metrics = {name: _summary(values, budgets[name]) for name, values in samples.items()}
    config_fragment = {
        "workload": {"market_rows": 5500, "candidate_rows": 360},
        "rounds": {"warmup": rounds.warmup, "measurement": rounds.measurement},
        "latency_p95_ms": dict(budgets),
    }
    config_hash = hashlib.sha256(
        json.dumps(config_fragment, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "v16_board_scoring_performance_v1",
        "passed": all(bool(metric["passed"]) for metric in metrics.values()),
        "config_hash": config_hash,
        "workload": manifest,
        "metrics": metrics,
        "sequential_reference": _distribution(sequential_samples),
        "sequential_wall_over_lane_wall": {
            "samples": len(speedup_samples),
            "p50": round(_nearest_rank(speedup_samples, 0.50), 3),
            "p95": round(_nearest_rank(speedup_samples, 0.95), 3),
            "maximum": round(max(speedup_samples), 3),
        },
        "lane_queue_wait": lane_status,
        "hit_rate": 0.0,
        "peak_items": peak_items,
        "process_cpu_seconds": round(time.process_time() - process_cpu_started, 6),
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
    }


def _atomic_write(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = _measure(args.config.resolve(), args.fixture.resolve())
    except Exception as exc:
        report = {
            "schema_version": "v16_board_scoring_performance_v1",
            "passed": False,
            "error": type(exc).__name__,
            "message": str(exc)[:300],
        }
        _atomic_write(args.output.resolve(), report)
        return 2
    _atomic_write(args.output.resolve(), report)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
