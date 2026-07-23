"""Deterministic, offline v17 performance acceptance runner."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
import tracemalloc
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from trader.application.board_scoring import BoardScoringCoordinator
from trader.application.board_scoring_cache import ScoringCacheContext
from trader.application.events import EventAuditRecord
from trader.application.published_snapshots import PublishedSnapshotIndex
from trader.application.publisher import SnapshotPublisher
from trader.application.queries import RecommendationQueries
from trader.domain.market.models import Board, FeatureSnapshot, LiveQuote, MarketQuote
from trader.domain.recommendation.models import (
    BoardStrategyPolicy,
    FusionMode,
    LiveOverlay,
    Recommendation,
    RecommendationAction,
    RecommendationSnapshot,
    ScoreBreakdown,
    Strategy,
)
from trader.domain.recommendation.ranking import SelectionPolicy, select_top_k
from trader.domain.recommendation.scoring import score_board_strategy
from trader.domain.recommendation.strategies.composition import LocalScoreResult
from trader.entrypoints.performance_recommendations import recommendation_operations
from trader.infra.market_data.columnar import ColumnarQuoteBatch, market_changes
from trader.infra.market_data.merge import (
    merge_market_observations,
    observation_from_quote,
    overlay_canonical_snapshot,
)
from trader.infra.market_data.normalize import MarketQuoteInput, build_market_quote, normalize_quotes
from trader.infra.settings import load_runtime_settings, load_strategy_settings
from trader.infra.settings_models import PerformanceBudgetSettings
from trader.web import create_app

_SUITE_METRICS = {
    "market-data": (
        "market_normalization",
        "market_merge",
        "canonical_snapshot",
        "targeted_overlay_commit",
    ),
    "board-scoring": (
        "board_preselection",
        "board_local_scoring",
        "three_strategy_board_scoring",
        "three_board_wall_clock",
        "global_selection",
    ),
    "api-sse": ("sse_delivery", "snapshot_api", "etag_api", "dates_api", "status_api"),
    "end-to-end": ("board_ready_to_draft", "quote_to_draft", "deepseek_to_hybrid"),
}


def run_performance_check(
    fixture_dir: Path,
    *,
    suite: str,
    budgets: PerformanceBudgetSettings,
    config_path: Path,
    baseline_path: Path | None = None,
) -> dict[str, object]:
    manifest_path = fixture_dir / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "fixture_id", "seed"}:
        raise ValueError("performance fixture manifest has an invalid schema")
    if raw["schema_version"] != 1 or not isinstance(raw["seed"], int):
        raise ValueError("performance fixture schema_version and seed are fixed")
    requested = tuple(_SUITE_METRICS) if suite == "all" else (suite,)
    metric_names = tuple(name for group in requested for name in _SUITE_METRICS[group])
    rows = _market_rows(budgets.workload.market_rows, int(raw["seed"]))
    operations, provenance = _operations(
        rows,
        budgets.workload.candidate_rows,
        metric_names=metric_names,
        config_path=config_path,
    )
    latency: dict[str, float] = {}
    for name in metric_names:
        operation = operations[name]
        for _ in range(budgets.rounds.warmup):
            operation()
        samples = [_elapsed_ms(operation) for _ in range(budgets.rounds.measurement)]
        latency[name] = _p95(samples)
    growth = _allocation_growth_percent(rows)
    absolute_failures = {
        name: {"actual_ms": actual, "budget_ms": budgets.latency_p95_ms[name]}
        for name, actual in latency.items()
        if actual > budgets.latency_p95_ms[name]
    }
    identity = {
        "fixture_id": raw["fixture_id"],
        "fixture_sha256": _tree_digest(fixture_dir),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "strategy_sha256": _strategy_digest(config_path),
        "python": platform.python_version(),
        "system": platform.system(),
        "kernel": platform.release(),
        "architecture": platform.machine(),
        "cpu": platform.processor() or "unknown",
    }
    relative_failures = _relative_failures(baseline_path, identity, latency, budgets.relative_regression_percent)
    source_root = Path(__file__).resolve().parents[2]
    result: dict[str, object] = {
        "schema_version": 1,
        "status": "failed"
        if absolute_failures or relative_failures or growth > budgets.memory.growth_percent
        else "passed",
        "suite": suite,
        "identity": identity,
        "source": {**_git_identity(source_root), "tree_sha256": _tree_digest(source_root)},
        "latency_p95_ms": latency,
        "workloads": {
            "market_rows": budgets.workload.market_rows,
            "candidate_rows": budgets.workload.candidate_rows,
            "candidate_quote_rows": 120,
            "topk_overlay_rows": 18,
            "strategies": 3,
        },
        "operation_provenance": {name: provenance[name] for name in metric_names},
        "memory": {
            "ticks": 100,
            "allocation_growth_percent": growth,
            "budget_percent": budgets.memory.growth_percent,
        },
        "absolute_failures": absolute_failures,
        "relative_failures": relative_failures,
        "network_calls": 0,
    }
    return result


def write_report(path: Path, report: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _operations(
    rows: list[dict[str, object]],
    candidate_rows: int,
    *,
    metric_names: tuple[str, ...],
    config_path: Path,
) -> tuple[dict[str, Callable[[], object]], dict[str, str]]:
    operations: dict[str, Callable[[], object]] = {}
    provenance = {
        "market_normalization": "trader.infra.market_data.normalize.normalize_quotes",
        "market_merge": "trader.infra.market_data.merge.merge_market_observations",
        "canonical_snapshot": "trader.infra.market_data.columnar.ColumnarQuoteBatch.from_snapshot",
        "targeted_overlay_commit": "trader.infra.market_data.merge.overlay_canonical_snapshot",
        "board_preselection": "trader.application.board_scoring.BoardScoringCoordinator.preselect",
        "board_local_scoring": "trader.domain.recommendation.scoring.score_board_strategy",
        "three_strategy_board_scoring": "trader.application.board_scoring.BoardScoringCoordinator.score",
        "three_board_wall_clock": "trader.application.board_scoring.BoardScoringCoordinator.score",
        "global_selection": "trader.domain.recommendation.ranking.select_top_k",
        "board_ready_to_draft": "trader.application.recommendations.RecommendationEngine.prepare_snapshot",
        "quote_to_draft": "trader.application.recommendations.RecommendationEngine.prepare_snapshot",
        "deepseek_to_hybrid": "trader.application.recommendations.RecommendationEngine.finalize_snapshot",
        "sse_delivery": "trader.application.publisher.SnapshotPublisher.publish_overlay",
        "snapshot_api": "trader.web.routes_recommendations.create_recommendation_blueprint",
        "etag_api": "trader.web.routes_recommendations.create_recommendation_blueprint",
        "dates_api": "trader.web.routes_recommendations.create_recommendation_blueprint",
        "status_api": "trader.web.routes_status.create_status_blueprint",
    }
    if any(name in _SUITE_METRICS["api-sse"] for name in metric_names):
        operations.update(_api_sse_operations())
    if any(name in _SUITE_METRICS["market-data"] for name in metric_names):
        operations.update(_market_data_operations(rows, candidate_rows))
    if any(name in _SUITE_METRICS["board-scoring"] for name in metric_names):
        operations.update(_board_scoring_operations(config_path))
    if any(name in _SUITE_METRICS["end-to-end"] for name in metric_names):
        operations.update(recommendation_operations(config_path))
    missing = set(metric_names).difference(operations)
    if missing:
        raise RuntimeError(f"production performance operations are missing: {sorted(missing)}")
    return operations, provenance


def _market_data_operations(
    rows: Sequence[Mapping[str, object]],
    candidate_rows: int,
) -> dict[str, Callable[[], object]]:
    observed_at = datetime(2026, 7, 23, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def normalize_source(
        source: str, price_multiplier: float, selected: Sequence[Mapping[str, object]]
    ) -> tuple[MarketQuote, ...]:
        def normalizer(row: Mapping[str, object], received_at: datetime) -> MarketQuote:
            return build_market_quote(
                MarketQuoteInput(
                    code=str(row["code"]),
                    name=f"fixture-{row['code']}",
                    price=float(str(row["price"])) * price_multiplier,
                    previous_close=float(str(row["price"])),
                    open_price=float(str(row["price"])),
                    high=float(str(row["price"])) * price_multiplier,
                    low=float(str(row["price"])),
                    pct_change=(price_multiplier - 1.0) * 100.0,
                    change_5m=0.5,
                    speed=0.2,
                    volume_ratio=1.5,
                    turnover_rate=2.0,
                    amount=200_000_000.0,
                    amplitude=2.0,
                    market_cap=20_000_000_000.0,
                    industry=f"industry-{int(str(row['code'])) % 20}",
                    source=source,
                    source_time=observed_at,
                    received_time=received_at,
                    data_version=f"{source}-fixture-v1",
                )
            )

        return normalize_quotes(selected, observed_at, normalizer=normalizer)

    east_quotes = normalize_source("eastmoney", 1.0, rows)
    sina_quotes = normalize_source("sina", 1.0001, rows)
    east_observations = tuple(
        observation_from_quote(quote, source="eastmoney", observed_at=observed_at) for quote in east_quotes
    )
    sina_observations = tuple(
        observation_from_quote(quote, source="sina", observed_at=observed_at) for quote in sina_quotes
    )
    base_snapshot = merge_market_observations((*east_observations, *sina_observations), observed_at=observed_at)
    base_batch = ColumnarQuoteBatch.from_snapshot(
        base_snapshot,
        config_version="performance-v1",
        schema_version="market-snapshot-v15",
    )
    targeted_rows = rows[:candidate_rows]
    tencent_quotes = normalize_source("tencent", 1.0002, targeted_rows)
    tencent_observations = tuple(
        observation_from_quote(quote, source="tencent", observed_at=observed_at) for quote in tencent_quotes
    )
    targeted_codes = tuple(quote.code for quote in tencent_quotes)

    def normalize() -> object:
        return normalize_source("eastmoney", 1.0, rows)

    def merge() -> object:
        return merge_market_observations((*east_observations, *sina_observations), observed_at=observed_at)

    def canonical() -> object:
        snapshot = merge_market_observations(
            (*east_observations, *sina_observations),
            observed_at=observed_at,
        )
        return ColumnarQuoteBatch.from_snapshot(
            snapshot,
            config_version="performance-v1",
            schema_version="market-snapshot-v15",
        )

    def targeted_commit() -> object:
        targeted = merge_market_observations(
            (*tencent_observations,),
            observed_at=observed_at,
            targeted_codes=targeted_codes,
        )
        committed = overlay_canonical_snapshot(base_snapshot, targeted)
        batch = ColumnarQuoteBatch.from_snapshot(
            committed,
            config_version="performance-v1",
            schema_version="market-snapshot-v15",
        )
        return market_changes(base_batch, batch)

    return {
        "market_normalization": normalize,
        "market_merge": merge,
        "canonical_snapshot": canonical,
        "targeted_overlay_commit": targeted_commit,
    }


def _board_scoring_operations(config_path: Path) -> dict[str, Callable[[], object]]:
    runtime = load_runtime_settings(config_path)
    settings = load_strategy_settings(runtime.strategy_config_path)
    observed_at = datetime(2026, 7, 23, 14, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    populations = {
        board: tuple(_board_feature(board, index, observed_at) for index in range(120))
        for board in (Board.MAIN, Board.CHINEXT, Board.STAR)
    }
    policies = {
        strategy: {
            board: BoardStrategyPolicy(
                policy_id=f"performance:{strategy.value}:{board.value}",
                version=settings.board_policy_version,
                board=board,
                strategy=strategy,
                candidate_weights=settings.board_candidate_weights[strategy.value][board.value],
                local_weights=settings.board_local_strategy_weights[strategy.value][board.value],
                candidate_min_score=settings.selection.candidate_min_score,
                minimum_reliability=settings.selection.minimum_board_reliability,
            )
            for board in populations
        }
        for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)
    }
    coordinator = BoardScoringCoordinator()
    context = ScoringCacheContext(
        observed_at.date().isoformat(),
        "afternoon",
        "performance-epoch",
        "performance-data-v1",
        observed_at,
    )
    all_features = tuple(feature for board_features in populations.values() for feature in board_features)
    selected = coordinator.preselect(
        Strategy.TOMORROW,
        populations[Board.MAIN],
        policies[Strategy.TOMORROW][Board.MAIN],
        context,
        limit=120,
    )

    def preselect() -> object:
        return coordinator.preselect(
            Strategy.TOMORROW,
            populations[Board.MAIN],
            policies[Strategy.TOMORROW][Board.MAIN],
            context,
            limit=120,
        )

    def local_score() -> object:
        policy = policies[Strategy.TOMORROW][Board.MAIN]
        return tuple(score_board_strategy(feature, policy) for feature in selected)

    def score_strategy(strategy: Strategy) -> tuple[Recommendation, ...]:
        batches = coordinator.score(
            strategy,
            all_features,
            policies[strategy],
            context,
            _score_fixture_recommendation,
        )
        return tuple(item for batch in batches for item in batch.recommendations)

    selection_candidates = score_strategy(Strategy.TOMORROW)

    def three_strategy() -> object:
        return tuple(score_strategy(strategy) for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25))

    def global_selection() -> object:
        return select_top_k(
            selection_candidates,
            SelectionPolicy(top_k=10, maximum_per_industry=3, maximum_board_fraction=0.6),
        )

    return {
        "board_preselection": preselect,
        "board_local_scoring": local_score,
        "three_strategy_board_scoring": three_strategy,
        "three_board_wall_clock": lambda: score_strategy(Strategy.TOMORROW),
        "global_selection": global_selection,
    }


def _board_feature(board: Board, index: int, observed_at: datetime) -> FeatureSnapshot:
    prefixes = {Board.MAIN: "600", Board.CHINEXT: "300", Board.STAR: "688"}
    code = f"{prefixes[board]}{index:03d}"
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
        source="offline-performance-fixture",
        source_time=observed_at,
        received_time=observed_at,
        data_version="performance-data-v1",
        board=board,
        board_source="fixture",
        board_reliability="verified",
        exchange="SSE" if board is not Board.CHINEXT else "SZSE",
        listing_date=date(2020, 1, 1),
        listing_age_sessions=1000,
        is_relisted_first_session=False,
        is_delisting_period_first_session=False,
        has_price_limit=True,
        exchange_limit_pct=10.0 if board is Board.MAIN else 20.0,
        strategy_hot_cap_pct=8.0 if board is Board.MAIN else 16.0,
        rule_version="performance-rule-v1",
        rule_effective_date=date(2026, 1, 1),
    )
    values = {
        "amount_median_20d": 200_000_000.0 + index * 1_000_000.0,
        "turnover_median_20d": 1.5,
        "return_1d": 2.0,
        "return_3d": 3.0,
        "return_5d": 5.0,
        "return_10d": 7.0,
        "return_20d": 10.0,
        "return_60d": 15.0,
        "volatility_20d": 2.0,
        "max_drawdown_20d": -8.0,
        "trend_score": 70.0,
        "ma20_60_position": 70.0,
        "ma20_60_structure": 70.0,
        "ma_slope": 70.0,
        "breakout_20d": 70.0,
        "industry_trend": 70.0,
        "tail_return_30m": 1.0,
        "tail_volume_ratio": 1.2,
        "close_location": 70.0,
        "capacity_score": 100.0,
        "moderate_amplitude": 100.0,
        "price_executability": 100.0,
        "limit_distance_safety": 70.0,
        "quality_score": 70.0,
        "value_score": 70.0,
        "growth_score": 70.0,
        "return_20d_not_overheated": 70.0,
        "atr20_pct": 2.0,
        "low_volatility_score": 70.0,
        "low_drawdown_score": 70.0,
        "market_breadth": 60.0,
        "market_regime_score": 50.0,
        "ma5": 12.0,
        "ma10": 11.8,
        "ma20": 11.5,
        "ma20_slope": 0.1,
        "high_20d_previous": 11.9,
    }
    return FeatureSnapshot(quote, values, observed_at, 60, merge_epoch="performance-epoch")


def _score_fixture_recommendation(
    strategy: Strategy,
    feature: FeatureSnapshot,
    _policy: BoardStrategyPolicy,
    local: LocalScoreResult,
) -> Recommendation:
    return Recommendation(
        strategy=strategy,
        features=feature,
        score=ScoreBreakdown(
            local.components,
            local.base_score,
            0.0,
            local.base_score,
            None,
            0.0,
            0.0,
            local.base_score,
            FusionMode.LOCAL_DEGRADED,
            False,
        ),
        local_risk_facts=(),
        deepseek_risk_facts=(),
        review=None,
        action=RecommendationAction.OBSERVE,
        action_reason="performance_fixture",
        veto=False,
    )


class _Archive(Protocol):
    def latest(self, strategy: Strategy) -> RecommendationSnapshot | None: ...

    def recommendation_dates(self, strategy: Strategy) -> tuple[str, ...]: ...

    def load_frozen(self, strategy: Strategy, trade_date: str) -> RecommendationSnapshot | None: ...

    def load_live_overlay(self, strategy: Strategy, trade_date: str) -> LiveOverlay | None: ...

    def list_events(self, *, cursor: int, limit: int) -> tuple[EventAuditRecord, ...]: ...


class _EmptyArchive:
    def latest(self, strategy: Strategy) -> RecommendationSnapshot | None:
        return None

    def recommendation_dates(self, strategy: Strategy) -> tuple[str, ...]:
        return ()

    def load_frozen(self, strategy: Strategy, trade_date: str) -> RecommendationSnapshot | None:
        return None

    def load_live_overlay(self, strategy: Strategy, trade_date: str) -> LiveOverlay | None:
        return None

    def list_events(self, *, cursor: int, limit: int) -> tuple[EventAuditRecord, ...]:
        return ()


def _api_sse_operations() -> dict[str, Callable[[], object]]:
    now = datetime(2026, 7, 23, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    archive: _Archive = _EmptyArchive()
    index = PublishedSnapshotIndex(archive)
    publisher = SnapshotPublisher(history_size=256, client_queue_size=16, now=lambda: now)
    base = _snapshot("perf-api-000", now)
    index.publish(base)
    publisher.publish(base)
    queries = RecommendationQueries(index, archive, now=lambda: now)
    app = create_app(
        lambda: {"schema_version": "v3", "status": "running", "runtime_started": True},
        queries=queries,
        publisher=publisher,
    )
    client = app.test_client()
    current_path = "/api/recommendations/today?view=current&top_n=18"
    initial = client.get(current_path)
    etag = initial.headers["ETag"]
    counter = 0

    def publish_overlay() -> object:
        nonlocal counter
        counter += 1
        observed_at = now + timedelta(microseconds=counter)
        return publisher.publish_overlay(
            LiveOverlay(
                snapshot_id=base.snapshot_id,
                strategy=base.strategy,
                trade_date=base.trade_date,
                version=f"perf-overlay-{counter:03d}",
                observed_at=observed_at,
                quotes={
                    item.features.quote.code: LiveQuote(
                        code=item.features.quote.code,
                        price=(item.features.quote.price or 0.0) + counter / 100.0,
                        pct_change=item.features.quote.pct_change,
                        source="offline-performance-fixture",
                        source_time=observed_at,
                        received_time=observed_at,
                        data_version=f"overlay:{counter:03d}",
                    )
                    for item in base.recommendations
                },
            )
        )

    return {
        "sse_delivery": publish_overlay,
        "snapshot_api": lambda: client.get(current_path),
        "etag_api": lambda: client.get(current_path, headers={"If-None-Match": etag}),
        "dates_api": lambda: client.get("/api/recommendation-dates?strategy=today"),
        "status_api": lambda: client.get("/api/status"),
    }


def _snapshot(snapshot_id: str, observed_at: datetime) -> RecommendationSnapshot:
    recommendations = tuple(
        _recommendation(_feature(f"600{index:03d}", observed_at, index), index) for index in range(1, 19)
    )
    return RecommendationSnapshot(
        snapshot_id=snapshot_id,
        strategy=Strategy.TODAY,
        trade_date=observed_at.date().isoformat(),
        phase="today_main",
        data_version=f"fixture:{snapshot_id}",
        strategy_version="strategy-v17-performance",
        fusion_version="fusion-v2",
        fusion_mode=FusionMode.LOCAL_DEGRADED,
        published_at=observed_at,
        recommendations=recommendations,
        filtered_count=342,
        filter_reasons={"hard_filter": 342},
        config_version="runtime-v17-performance",
    )


def _feature(code: str, observed_at: datetime, index: int) -> FeatureSnapshot:
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
        speed=0.5,
        volume_ratio=2.0,
        turnover_rate=2.5,
        amount=300_000_000.0,
        amplitude=4.0,
        market_cap=30_000_000_000.0,
        industry=f"industry-{index % 6}",
        source="offline-performance-fixture",
        source_time=observed_at,
        received_time=observed_at,
        data_version=f"quote:{code}",
    )
    return FeatureSnapshot(quote=quote, values={}, observed_at=observed_at, history_days=60)


def _recommendation(feature: FeatureSnapshot, rank: int) -> Recommendation:
    action = RecommendationAction.EXECUTABLE if rank <= 10 else RecommendationAction.OBSERVE
    local_score = 82.0 - rank / 10.0
    return Recommendation(
        strategy=Strategy.TODAY,
        features=feature,
        score=ScoreBreakdown(
            {},
            local_score,
            0.0,
            local_score,
            None,
            0.0,
            0.0,
            local_score,
            FusionMode.LOCAL_DEGRADED,
            False,
        ),
        local_risk_facts=(),
        deepseek_risk_facts=(),
        review=None,
        action=action,
        action_reason="threshold_met" if action is RecommendationAction.EXECUTABLE else "near_threshold",
        veto=False,
        rank=rank,
    )


def _market_rows(count: int, seed: int) -> list[dict[str, object]]:
    return [
        {
            "code": f"{index:06d}",
            "price": float((index + seed) % 10000) / 100 + 1.0,
            "score": float((index * 17 + seed) % 10000) / 100,
            "board": ("main", "chinext", "star")[index % 3],
        }
        for index in range(count)
    ]


def _elapsed_ms(operation: Callable[[], object]) -> float:
    started = time.perf_counter_ns()
    operation()
    return (time.perf_counter_ns() - started) / 1_000_000


def _p95(samples: list[float]) -> float:
    if len(samples) == 1:
        return round(samples[0], 6)
    return round(statistics.quantiles(samples, n=100, method="inclusive")[94], 6)


def _allocation_growth_percent(rows: list[dict[str, object]]) -> float:
    tracemalloc.start()
    retained: list[object] = []
    first = 0
    for tick in range(100):
        retained[:] = [tuple(row.values()) for row in rows[:120]]
        current, _peak = tracemalloc.get_traced_memory()
        if tick == 9:
            first = current
    final, _peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return round(max(0.0, (final - first) / max(1, first) * 100.0), 6)


def _relative_failures(
    baseline_path: Path | None,
    identity: Mapping[str, object],
    latency: Mapping[str, float],
    limit_percent: float,
) -> dict[str, object]:
    if baseline_path is None:
        return {}
    raw = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline_identity = raw.get("identity") if isinstance(raw, dict) else None
    if baseline_identity != identity:
        raise ValueError("baseline identity does not match fixture, config and environment")
    baseline = raw.get("latency_p95_ms")
    if not isinstance(baseline, dict):
        raise ValueError("baseline latency_p95_ms is missing")
    failures: dict[str, object] = {}
    for name, actual in latency.items():
        previous = float(baseline[name])
        regression = (actual - previous) / max(previous, 0.000001) * 100.0
        if regression > limit_percent:
            failures[name] = {"actual_ms": actual, "baseline_ms": previous, "regression_percent": regression}
    return failures


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file() and "__pycache__" not in item.parts):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _strategy_digest(config_path: Path) -> str:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    strategy_name = raw.get("strategy_config") if isinstance(raw, dict) else None
    if not isinstance(strategy_name, str) or not strategy_name:
        raise ValueError("runtime strategy_config is missing")
    strategy_path = Path(strategy_name)
    if not strategy_path.is_absolute():
        strategy_path = config_path.parent / strategy_path
    return hashlib.sha256(strategy_path.read_bytes()).hexdigest()


def _git_identity(root: Path) -> dict[str, object]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True, timeout=2
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"], cwd=root, check=True, capture_output=True, text=True, timeout=2
            ).stdout.strip()
        )
    except (OSError, subprocess.SubprocessError):
        commit, dirty = "unavailable", None
    return {"commit": commit, "dirty": dirty, "executable": sys.executable}


__all__ = ["run_performance_check", "write_report"]
