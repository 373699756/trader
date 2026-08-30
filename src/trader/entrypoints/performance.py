"""Deterministic offline performance gate over active V2 production functions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast

from trader.application.cache import canonical_json_bytes
from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.decision_drafts import UnifiedDecisionDraftIndex
from trader.application.decision_events import build_v2_decision_committed
from trader.application.decision_queries import UnifiedDecisionQueries
from trader.application.decision_stream import UnifiedDecisionEventStream
from trader.application.policy import RecommendationPolicy
from trader.application.ports.scored import TomorrowNativeInput
from trader.application.schedule import SHANGHAI
from trader.application.scored_v2_projection import (
    ScoredV2LocalProjection,
    build_scored_v2_hybrid,
    build_scored_v2_local,
)
from trader.application.tomorrow_model_scoring import TomorrowProductionModelScoringService
from trader.bootstrap_policy import _recommendation_policy
from trader.domain.market.models import Board, FeatureSnapshot, MarketQuote
from trader.domain.recommendation.decision_identity import (
    CommittedDecisionRecord,
    DecisionItem,
    DecisionOverlay,
    DecisionQuote,
    ScoredDecision,
)
from trader.domain.recommendation.models import RecommendationAction, Strategy
from trader.domain.recommendation.ranking import candidate_score
from trader.domain.recommendation.scoring import score_board_strategy
from trader.domain.recommendation.strategies.composition import LocalScoreResult
from trader.domain.review.models import DeepSeekReview, ReviewOutcome
from trader.infra.market_data.columnar import ColumnarQuoteBatch, targeted_market_changes
from trader.infra.market_data.merge import (
    merge_market_observations,
    observation_from_quote,
    overlay_canonical_snapshot,
)
from trader.infra.market_data.normalize import MarketQuoteInput, build_market_quote
from trader.infra.market_data.observations import SourceObservation
from trader.infra.settings import load_runtime_settings, load_strategy_settings
from trader.infra.settings_models import PerformanceBudgetSettings
from trader.infra.tomorrow_production_model import load_packaged_tomorrow_production_model
from trader.web import create_app
from trader.web.route_services import UnifiedWebServices


@dataclass(frozen=True)
class _OperationContext:
    config_version: str
    policy: RecommendationPolicy
    tomorrow_model: TomorrowProductionModelScoringService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline", type=Path)
    return parser


def run(config_path: Path, *, baseline_path: Path | None = None) -> dict[str, object]:
    settings = load_runtime_settings(config_path)
    strategy_settings = load_strategy_settings(settings.strategy_config_path)
    budgets = settings.performance_budgets
    market_inputs, market_quotes, candidates = _fixtures(budgets)
    context = _OperationContext(
        settings.config_version,
        _recommendation_policy(strategy_settings),
        TomorrowProductionModelScoringService(
            load_packaged_tomorrow_production_model(strategy_settings.tomorrow_scoring_profile)
        ),
    )
    operations, provenance = _operations(
        market_inputs,
        market_quotes,
        candidates,
        context,
    )
    measurements = {
        name: _measure(operation, budgets.rounds.warmup, budgets.rounds.measurement)
        for name, operation in operations.items()
    }
    baseline = _baseline(baseline_path)
    failures = _failures(measurements, budgets, baseline)
    rss_before = _rss_kib()
    for _ in range(100):
        operations["three_strategy_board_scoring"]()
    rss_after = _rss_kib()
    peak_bytes = rss_after * 1024
    growth_percent = 0.0 if rss_before == 0 else max(0.0, (rss_after - rss_before) / rss_before * 100.0)
    if peak_bytes > budgets.memory.process_peak_rss_bytes:
        failures.append("process_peak_rss:absolute_budget")
    if growth_percent > budgets.memory.growth_percent:
        failures.append("process_rss_growth:absolute_budget")
    fixture_hash = hashlib.sha256(
        "\n".join(f"{item.code}:{item.data_version}" for item in market_inputs).encode()
    ).hexdigest()
    source_root = Path(__file__).resolve().parents[2]
    return {
        "schema_version": "v2_production_performance_v2",
        "status": "passed" if not failures else "failed",
        "identity": {
            "config_version": settings.config_version,
            "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "fixture_sha256": fixture_hash,
            "source_sha256": _source_digest(source_root),
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "system": platform.system(),
            "kernel": platform.release(),
            "architecture": platform.machine(),
            "cpu": platform.processor() or "unknown",
            "cpu_count": os.cpu_count(),
        },
        "workload": {
            "market_rows": len(market_inputs),
            "candidate_rows": len(candidates),
            "strategies": 3,
            "warmup_rounds": budgets.rounds.warmup,
            "measurement_rounds": budgets.rounds.measurement,
            "tick_count": 100,
        },
        "measurements": measurements,
        "operation_provenance": provenance,
        "memory": {
            "rss_before_kib": rss_before,
            "rss_after_kib": rss_after,
            "growth_percent": round(growth_percent, 3),
            "peak_budget_bytes": budgets.memory.process_peak_rss_bytes,
        },
        "relative_regression_percent": budgets.relative_regression_percent,
        "network_calls": 0,
        "external_checks": {
            "browser_patch_to_paint": "scripts/diagnose_runtime.py --profile browser",
            "supplier_realtime": "scripts/diagnose_runtime.py --profile tencent",
        },
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run(args.config.resolve(), baseline_path=args.baseline.resolve() if args.baseline else None)
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output is not None:
        args.output.write_text(f"{payload}\n", encoding="utf-8")
    print(payload)
    return 0 if report["status"] == "passed" else 1


def _operations(
    market_inputs: tuple[MarketQuoteInput, ...],
    market_quotes: tuple[MarketQuote, ...],
    candidates: tuple[FeatureSnapshot, ...],
    context: _OperationContext,
) -> tuple[dict[str, Callable[[], object]], dict[str, str]]:
    config_version = context.config_version
    policy = context.policy
    tomorrow_model = context.tomorrow_model
    observed_at = market_quotes[0].received_time
    observations = _complete_realtime_observations(market_quotes, observed_at)
    merged = merge_market_observations(observations, observed_at=observed_at)
    changed_quotes = tuple(
        replace(
            quote,
            price=(quote.price or 0.0) + 0.01,
            source_time=observed_at + timedelta(milliseconds=1),
            received_time=observed_at + timedelta(milliseconds=1),
            data_version=f"{quote.data_version}:overlay",
        )
        for quote in market_quotes[:18]
    )
    changed_at = observed_at + timedelta(milliseconds=1)
    overlay_snapshot = merge_market_observations(
        tuple(
            observation_from_quote(quote, source="performance_overlay", observed_at=changed_at)
            for quote in changed_quotes
        ),
        observed_at=changed_at,
        targeted_codes=tuple(quote.code for quote in changed_quotes),
    )
    committed_overlay = overlay_canonical_snapshot(merged, overlay_snapshot)
    market_features = tuple(
        FeatureSnapshot(quote, candidates[index % len(candidates)].values, observed_at, 61)
        for index, quote in enumerate(market_quotes)
    )
    tomorrow_input = TomorrowNativeInput(
        observed_at.date(),
        "afternoon",
        "performance-data-v2",
        config_version,
        observed_at,
        market_features,
        tuple(feature.quote.code for feature in candidates),
        candidates,
        30.0,
        30.0,
        len(candidates),
    )
    candidate_input = replace(
        tomorrow_input,
        data_version="performance-candidate-data-v2",
        market_features=candidates,
    )
    local_projection = build_scored_v2_local(
        tomorrow_input,
        policy,
        sequence=1,
        tomorrow_model=tomorrow_model,
    )
    reviews = _abstaining_reviews(local_projection, observed_at)
    api_operations = _api_operations(candidates, observed_at)
    overlay_commit = _overlay_cas_operation(candidates, observed_at)

    def tomorrow_projection() -> object:
        return build_scored_v2_local(
            tomorrow_input,
            policy,
            sequence=1,
            tomorrow_model=tomorrow_model,
        )

    def candidate_projection() -> object:
        return build_scored_v2_local(
            candidate_input,
            policy,
            sequence=1,
            tomorrow_model=tomorrow_model,
        )

    def active_score(strategy: Strategy, item: FeatureSnapshot) -> LocalScoreResult:
        board_policy = policy.board_policy(strategy, item.quote.board)
        if board_policy is None:
            raise ValueError(f"missing board policy for {strategy.value}/{item.quote.board.value}")
        return score_board_strategy(item, board_policy)

    operations: dict[str, Callable[[], object]] = {
        "market_normalization": lambda: tuple(build_market_quote(item) for item in market_inputs),
        "market_merge": lambda: merge_market_observations(observations, observed_at=observed_at),
        "canonical_snapshot": lambda: ColumnarQuoteBatch.from_snapshot(
            merged,
            config_version=config_version,
            schema_version="performance-market-v2",
        ),
        "targeted_overlay_commit": lambda: (
            overlay_canonical_snapshot(merged, overlay_snapshot),
            targeted_market_changes(merged, committed_overlay, tuple(quote.code for quote in changed_quotes)),
            overlay_commit(),
        ),
        "board_preselection": lambda: sorted(
            candidates,
            key=lambda item: candidate_score(item, _CANDIDATE_WEIGHTS),
            reverse=True,
        ),
        "board_local_scoring": lambda: tuple(active_score(Strategy.TOMORROW, item) for item in candidates),
        "three_strategy_board_scoring": lambda: tuple(
            tuple(active_score(strategy, item) for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25))
            for item in candidates
        ),
        "three_board_wall_clock": lambda: tuple(
            tuple(active_score(Strategy.TOMORROW, item) for item in candidates if item.quote.board is board)
            for board in (Board.MAIN, Board.CHINEXT, Board.STAR)
        ),
        "global_selection": lambda: sorted(
            (active_score(Strategy.TOMORROW, item).base_score for item in candidates),
            reverse=True,
        )[:6],
        "board_ready_to_draft": candidate_projection,
        "quote_to_draft": tomorrow_projection,
        "deepseek_to_hybrid": lambda: build_scored_v2_hybrid(
            local_projection,
            policy,
            reviews,
            review_deadline=observed_at + timedelta(minutes=18),
        ),
        **api_operations,
    }
    provenance = {
        "market_normalization": "trader.infra.market_data.normalize.build_market_quote",
        "market_merge": "trader.infra.market_data.merge.merge_market_observations",
        "canonical_snapshot": "trader.infra.market_data.columnar.ColumnarQuoteBatch.from_snapshot",
        "targeted_overlay_commit": "trader.infra.market_data.merge.overlay_canonical_snapshot + trader.application.decision_core.UnifiedDecisionIndex.publish_overlay",
        "board_preselection": "trader.domain.recommendation.ranking.candidate_score",
        "board_local_scoring": "trader.domain.recommendation.scoring.score_board_strategy",
        "three_strategy_board_scoring": "trader.domain.recommendation.scoring.score_board_strategy",
        "three_board_wall_clock": "trader.domain.recommendation.scoring.score_board_strategy",
        "global_selection": "trader.domain.recommendation.scoring.score_board_strategy",
        "board_ready_to_draft": "trader.application.scored_v2_projection.build_scored_v2_local",
        "quote_to_draft": "trader.application.scored_v2_projection.build_scored_v2_local",
        "deepseek_to_hybrid": "trader.application.scored_v2_projection.build_scored_v2_hybrid",
        "sse_publish": "trader.application.decision_stream.UnifiedDecisionEventStream.publish_committed",
        "snapshot_api": "trader.web.routes_v2._current",
        "etag_api": "trader.web.routes_v2._current",
        "dates_api": "trader.web.routes_v2._dates",
        "status_api": "trader.web.routes_v2._status",
    }
    return operations, provenance


def _api_operations(
    candidates: tuple[FeatureSnapshot, ...],
    observed_at: datetime,
) -> dict[str, Callable[[], object]]:
    index = UnifiedDecisionIndex()
    drafts = UnifiedDecisionDraftIndex()
    stream = UnifiedDecisionEventStream(history_size=256, client_queue_size=16)
    decision = _performance_decision(candidates, observed_at, sequence=1)
    overlay = DecisionOverlay(
        decision.strategy,
        decision.trade_date,
        decision.version,
        decision.observed_at,
        tuple(item.quote for item in decision.items if item.quote is not None),
    )
    if not index.publish_scored(decision, overlay, expected_version=None).accepted:
        raise RuntimeError("performance decision setup failed")
    stream.publish_committed(build_v2_decision_committed(decision))
    queries = UnifiedDecisionQueries(index, drafts, _EmptyHistory(), _FixedClock(observed_at))
    app = create_app(
        services=UnifiedWebServices(
            queries,
            stream,
            lambda: {
                "status": "running",
                "runtime_started": True,
                "phase": "afternoon",
                "deepseek_budget": {"used": 0, "remaining": 168},
            },
        )
    )
    client = app.test_client()
    path = "/api/v2/decisions/tomorrow/current"
    etag = client.get(path).headers["ETag"]
    event = build_v2_decision_committed(decision)
    return {
        "sse_publish": lambda: stream.publish_committed(event),
        "snapshot_api": lambda: client.get(path),
        "etag_api": lambda: client.get(path, headers={"If-None-Match": etag}),
        "dates_api": lambda: client.get("/api/v2/decisions/tomorrow/dates"),
        "status_api": lambda: client.get("/api/v2/status"),
    }


def _overlay_cas_operation(
    candidates: tuple[FeatureSnapshot, ...],
    observed_at: datetime,
) -> Callable[[], object]:
    index = UnifiedDecisionIndex()
    decision = _performance_decision(candidates, observed_at, sequence=1)
    initial = DecisionOverlay(
        decision.strategy,
        decision.trade_date,
        decision.version,
        observed_at,
        tuple(item.quote for item in decision.items if item.quote is not None),
    )
    if not index.publish_scored(decision, initial, expected_version=None).accepted:
        raise RuntimeError("performance overlay setup failed")
    counter = 0

    def commit() -> object:
        nonlocal counter
        counter += 1
        current = index.snapshot(decision.strategy).overlay
        if current is None:
            raise RuntimeError("performance overlay is unavailable")
        at = observed_at + timedelta(milliseconds=counter)
        quotes = tuple(
            replace(quote, price=quote.price + counter / 10000.0, source_time=at, data_version=f"overlay:{counter}")
            for quote in current.quotes
        )
        overlay = DecisionOverlay(decision.strategy, decision.trade_date, decision.version, at, quotes)
        return index.publish_overlay(overlay, expected_version=current.version)

    return commit


def _performance_decision(
    candidates: tuple[FeatureSnapshot, ...],
    observed_at: datetime,
    *,
    sequence: int,
) -> ScoredDecision:
    items: list[DecisionItem] = []
    for rank, feature in enumerate(candidates[:12], start=1):
        quote = feature.quote
        if quote.price is None:
            raise RuntimeError("performance quote price is unavailable")
        items.append(
            DecisionItem(
                quote.code,
                RecommendationAction.EXECUTABLE if rank <= 6 else RecommendationAction.OBSERVE,
                True,
                rank,
                90.0 - rank,
                85.0 - rank / 10.0,
                85.0 - rank / 10.0,
                (("local_score", 85.0 - rank / 10.0),),
                (),
                "threshold_met" if rank <= 6 else "near_threshold",
                quote.name,
                quote.industry,
                DecisionQuote(
                    quote.code,
                    quote.price,
                    quote.pct_change,
                    quote.amount,
                    quote.turnover_rate,
                    quote.market_cap,
                    quote.source,
                    quote.source_time,
                    quote.data_version,
                ),
            )
        )
    return ScoredDecision(
        Strategy.TOMORROW,
        observed_at.date(),
        sequence,
        observed_at,
        "local",
        None,
        (("market", f"performance:{sequence}"),),
        "config:performance",
        "strategy:performance",
        "fusion:performance",
        tuple(items),
        (("hard_filter", 348),),
        population_count=360,
        rejected_count=348,
    )


def _abstaining_reviews(
    projection: ScoredV2LocalProjection,
    observed_at: datetime,
) -> dict[str, DeepSeekReview]:
    return {
        candidate.code: DeepSeekReview(
            candidate.code,
            ReviewOutcome.ABSTAIN,
            {},
            (),
            observed_at + timedelta(seconds=1),
            error="performance_abstain",
        )
        for candidate in projection.review_candidates
    }


def _fixtures(
    budgets: PerformanceBudgetSettings,
) -> tuple[tuple[MarketQuoteInput, ...], tuple[MarketQuote, ...], tuple[FeatureSnapshot, ...]]:
    now = datetime(2026, 8, 25, 10, 0, tzinfo=SHANGHAI)
    market = tuple(_quote_input(index, now) for index in range(budgets.workload.market_rows))
    quotes = tuple(_complete_quote(build_market_quote(item), now) for item in market)
    candidate_indexes = (
        *range(0, 120),
        *range(2500, 2620),
        *range(4000, 4120),
    )
    selected = tuple(candidate_indexes[: budgets.workload.candidate_rows])
    candidates = tuple(
        FeatureSnapshot(
            quotes[index],
            _performance_feature_values(position),
            now,
            history_days=61,
        )
        for position, index in enumerate(selected)
    )
    return market, quotes, candidates


def _performance_feature_values(position: int) -> dict[str, float]:
    offset = (position % 120) / 10_000.0
    return {
        **_FEATURE_VALUES,
        "p2_return_1d": 0.002 + offset,
        "p2_return_3d": 0.004 + offset,
        "p2_return_5d": 0.006 + offset,
        "p2_momentum_20d_skip5": 0.01 + offset,
        "p2_momentum_40d_skip5": 0.02 + offset,
        "p2_momentum_60d_skip5": 0.03 + offset,
        "p2_amihud_20d": 0.0001 + position / 1_000_000.0,
        "p2_average_amount_20d": 100_000_000.0 + position * 100_000.0,
    }


_COMPLETE_REALTIME_FIELDS = (
    "name",
    "price",
    "previous_close",
    "open_price",
    "high",
    "low",
    "pct_change",
    "change_5m",
    "speed",
    "volume_ratio",
    "turnover_rate",
    "amount",
    "amplitude",
    "market_cap",
    "industry",
    "is_st",
    "is_suspended",
    "is_one_price_limit",
    "is_blacklisted",
    "has_major_regulatory_risk",
)


def _complete_realtime_observations(
    quotes: tuple[MarketQuote, ...],
    observed_at: datetime,
) -> tuple[SourceObservation, ...]:
    """Build the two admitted full-market provider batches used in production merge."""

    observations: list[SourceObservation] = []
    for quote in quotes:
        base = observation_from_quote(quote, source="eastmoney", observed_at=observed_at)
        fields = {name: base.fields[name] for name in _COMPLETE_REALTIME_FIELDS}
        for source in ("eastmoney", "sina"):
            data_version = f"{source}:{quote.data_version}"
            observations.append(
                replace(
                    base,
                    source=source,
                    data_version=data_version,
                    fields=fields,
                    payload_hash=hashlib.sha256(canonical_json_bytes(fields)).hexdigest(),
                )
            )
    return tuple(observations)


def _quote_input(index: int, now: datetime) -> MarketQuoteInput:
    if index < 2500:
        board, code = Board.MAIN, f"{600000 + index:06d}"
    elif index < 4000:
        board, code = Board.CHINEXT, f"{300000 + index - 2500:06d}"
    else:
        board, code = Board.STAR, f"{688000 + index - 4000:06d}"
    return MarketQuoteInput(
        code,
        f"样本{index}",
        10.0 + index / 10000.0,
        10.0,
        10.0,
        10.2,
        9.8,
        1.0,
        0.1,
        0.1,
        1.2,
        2.0,
        100_000_000.0,
        4.0,
        5_000_000_000.0,
        f"样本行业{index % 20}",
        "eastmoney",
        now,
        now,
        f"fixture:{index}",
        board=board,
        board_source="performance_fixture",
        board_reliability="verified",
        exchange="SZSE" if board is Board.CHINEXT else "SSE",
        listing_date=date(2020, 1, 1),
    )


def _complete_quote(quote: MarketQuote, now: datetime) -> MarketQuote:
    return replace(
        quote,
        listing_age_sessions=1400,
        is_relisted_first_session=False,
        is_delisting_period_first_session=False,
        has_price_limit=True,
        exchange_limit_pct=10.0 if quote.board is Board.MAIN else 20.0,
        strategy_hot_cap_pct=8.0 if quote.board is Board.MAIN else 16.0,
        rule_version="performance-rule-v1",
        rule_effective_date=now.date(),
    )


def _measure(operation: Callable[[], object], warmup: int, rounds: int) -> dict[str, object]:
    for _ in range(warmup):
        operation()
    values: list[float] = []
    for _ in range(rounds):
        started = time.perf_counter()
        operation()
        values.append((time.perf_counter() - started) * 1000.0)
    ordered = sorted(values)
    p95 = ordered[max(0, min(len(ordered) - 1, round(len(ordered) * 0.95) - 1))]
    return {"p50_ms": round(statistics.median(values), 3), "p95_ms": round(p95, 3), "samples": rounds}


def _baseline(path: Path | None) -> dict[str, object]:
    if path is None:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("measurements"), dict):
        raise ValueError("performance baseline must contain measurements")
    return cast(dict[str, object], raw["measurements"])


def _failures(
    measurements: dict[str, dict[str, object]],
    budgets: PerformanceBudgetSettings,
    baseline: dict[str, object],
) -> list[str]:
    failures: list[str] = []
    for name, limit in budgets.latency_p95_ms.items():
        if name == "browser_patch_to_paint":
            continue
        measured = measurements.get(name)
        measured_p95 = measured.get("p95_ms") if measured is not None else None
        if not isinstance(measured_p95, (int, float)) or isinstance(measured_p95, bool) or measured_p95 > limit:
            failures.append(f"{name}:absolute_budget")
            continue
        previous = baseline.get(name)
        if isinstance(previous, Mapping) and isinstance(previous.get("p95_ms"), (int, float)):
            allowed = float(previous["p95_ms"]) * (1.0 + budgets.relative_regression_percent / 100.0)
            if measured_p95 > allowed:
                failures.append(f"{name}:relative_regression")
    return failures


def _source_digest(source_root: Path) -> str:
    digest = hashlib.sha256()
    paths = sorted((*source_root.rglob("*.py"), *source_root.rglob("*.js")), key=lambda item: item.as_posix())
    for path in paths:
        digest.update(path.relative_to(source_root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _rss_kib() -> int:
    if os.name == "nt":
        return _windows_peak_working_set_kib()
    import resource

    peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return peak // 1024 if sys.platform == "darwin" else peak


def _windows_peak_working_set_kib() -> int:
    import ctypes
    from ctypes import wintypes

    size_t = ctypes.c_size_t

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = (
            ("cb", wintypes.DWORD),
            ("page_fault_count", wintypes.DWORD),
            ("peak_working_set_size", size_t),
            ("working_set_size", size_t),
            ("quota_peak_paged_pool_usage", size_t),
            ("quota_paged_pool_usage", size_t),
            ("quota_peak_non_paged_pool_usage", size_t),
            ("quota_non_paged_pool_usage", size_t),
            ("pagefile_usage", size_t),
            ("peak_pagefile_usage", size_t),
        )

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    win_dll = ctypes.__dict__["WinDLL"]
    kernel32 = win_dll("kernel32", use_last_error=True)
    psapi = win_dll("psapi", use_last_error=True)
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.restype = wintypes.HANDLE
    get_process_memory_info = psapi.GetProcessMemoryInfo
    get_process_memory_info.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ProcessMemoryCounters),
        wintypes.DWORD,
    )
    get_process_memory_info.restype = wintypes.BOOL
    if not get_process_memory_info(get_current_process(), ctypes.byref(counters), counters.cb):
        raise ctypes.__dict__["WinError"](ctypes.__dict__["get_last_error"]())
    return int(counters.peak_working_set_size // 1024)


class _FixedClock:
    def __init__(self, value: datetime) -> None:
        self._value = value

    def now(self) -> datetime:
        return self._value


class _EmptyHistory:
    def load(self, strategy: Strategy, trade_date: date) -> CommittedDecisionRecord | None:
        del strategy, trade_date
        return None

    def list_dates(self, strategy: Strategy, *, limit: int = 31) -> tuple[date, ...]:
        del strategy, limit
        return ()


_CANDIDATE_WEIGHTS = {
    "liquidity": 0.25,
    "short_momentum": 0.25,
    "trend": 0.25,
    "data_completeness": 0.25,
}

_FEATURE_VALUES = {
    "liquidity_score": 60.0,
    "short_momentum_score": 60.0,
    "trend_score": 70.0,
    "data_completeness_score": 100.0,
    "amount_median_20d": 200_000_000.0,
    "turnover_median_20d": 1.5,
    "return_1d": 2.0,
    "return_3d": 3.0,
    "return_5d": 5.0,
    "return_10d": 7.0,
    "return_20d": 10.0,
    "return_60d": 15.0,
    "volatility_20d": 2.0,
    "max_drawdown_20d": -8.0,
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


if __name__ == "__main__":
    sys.exit(main())
