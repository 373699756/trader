"""Bounded live quote comparisons with fixed qfq inputs and isolated production scoring."""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import sys
import time
from collections import Counter
from collections.abc import Sequence
from contextlib import redirect_stdout
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from trader.infra.market_data.observations import SourceObservation
from trader.infra.market_data.providers.baostock_industry import BaoStockIndustryClient
from trader.infra.market_data.providers.exchange_security_master import (
    ExchangeSecurityMasterClient,
    fetch_sse_listings,
    fetch_szse_listings,
)
from trader.infra.settings import load_runtime_settings, load_strategy_settings
from trader.infra.workers import BoundedExecutor
from trader.recommendation.application.pipeline.candidate_pool.candidate_builder import (
    CandidatePlanningContext,
    build_candidate_plans,
)
from trader.recommendation.application.pipeline.final_selection.decision_projection import (
    ScoredProjectionInputs,
    build_scored_local,
)
from trader.recommendation.application.ports.scoring import D25NativeInput, TomorrowNativeInput
from trader.recommendation.domain.market.models import FeatureSnapshot, MarketQuote, ModelIndustryReference
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.scoring import score_board_strategy
from trader.recommendation.infra.market_data.gateway import MarketDataGateway
from trader.recommendation.infra.market_data.providers.sina import SinaClient
from trader.recommendation.infra.market_data.providers.tencent import TencentClient, TencentFullMarketSource
from trader.recommendation.infra.normalization.merge import merge_market_observations, observation_from_quote

from .market_factor_audit import factor_audit, missing_quote_codes, quote_audit
from .market_factor_auxiliary import AuxiliaryInputs, probe_auxiliary
from .market_factor_eastmoney import fetch_eastmoney_quotes
from .market_factor_inputs import (
    FactorInputs,
    FixedReferences,
    attach_model_industries,
    load_factor_inputs,
    read_references,
)
from .reporting import emit_report, summarize_latency_ms

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SOURCES = ("tencent", "eastmoney", "sina")


@dataclass(frozen=True)
class SourceAttempt:
    source: str
    quotes: tuple[MarketQuote, ...]
    elapsed_ms: float
    physical_rows: int
    error: str | None


@dataclass(frozen=True)
class CollectedBatch:
    attempts: tuple[SourceAttempt, ...]
    requested_gaps: tuple[str, ...]
    received_at: datetime
    acquisition_ms: float


def acquire_batch(
    sources: tuple[str, ...], codes: tuple[str, ...], pool: BoundedExecutor, args: argparse.Namespace
) -> CollectedBatch:
    started = time.monotonic()
    deadline = started + args.batch_seconds
    attempts = [fetch_source(sources[0], codes, pool, args.timeout_seconds, deadline)]
    missing = missing_quote_codes(attempts[0].quotes, codes, _now())
    if len(sources) > 1 and missing:
        attempts.append(fetch_source(sources[1], missing, pool, args.timeout_seconds, deadline))
    return CollectedBatch(tuple(attempts), missing, _now(), _elapsed(started))


def _now() -> datetime:
    return datetime.now(_SHANGHAI)


def _elapsed(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 3)


def select_codes(official: Sequence[SourceObservation], size: int) -> tuple[str, ...]:
    """Round-robin boards so small probes do not accidentally sample one board."""
    groups = [
        sorted(item.subject_key for item in official if item.fields.get("board") == board)
        for board in ("main", "chinext", "star")
    ]
    ordered = tuple(
        groups[board][index]
        for index in range(max(map(len, groups), default=0))
        for board in range(3)
        if index < len(groups[board])
    )
    return ordered if size == 0 else ordered[:size]


def fetch_source(
    source: str, codes: tuple[str, ...], pool: BoundedExecutor, timeout: float, deadline: float
) -> SourceAttempt:
    started = time.monotonic()
    remaining = deadline - started
    if remaining <= 0:
        return SourceAttempt(source, (), 0.0, 0, "batch_deadline")
    end_at = _now() + timedelta(seconds=remaining)
    options = {"timeout_seconds": min(timeout, remaining), "session_factory": requests.Session, "worker_pool": pool}
    try:
        if source == "tencent":
            quotes = TencentClient(**options).fetch_quotes(codes, deadline=end_at)
        elif source == "eastmoney":
            quotes = fetch_eastmoney_quotes(min(timeout, remaining), deadline, pool)
        elif source == "sina":
            quotes = SinaClient(**options).fetch_market(deadline=end_at)
        else:
            raise ValueError("unknown comparison source")
        physical_rows = len(quotes)
        allowed = set(codes)
        quotes = tuple(quote for quote in quotes if quote.code in allowed)
        if time.monotonic() > deadline:
            return SourceAttempt(source, (), _elapsed(started), physical_rows, "batch_deadline")
        return SourceAttempt(source, quotes, _elapsed(started), physical_rows, None)
    except (OSError, RuntimeError, ValueError, requests.RequestException) as exc:
        return SourceAttempt(source, (), _elapsed(started), 0, type(exc).__name__)


def _attempt_report(attempt: SourceAttempt, codes: tuple[str, ...], at: datetime) -> dict[str, object]:
    return {
        "source": attempt.source,
        "elapsed_ms": attempt.elapsed_ms,
        "error": attempt.error,
        "physical_rows": attempt.physical_rows,
        "audit": quote_audit(attempt.quotes, codes, at),
    }


def _score(
    features: tuple[FeatureSnapshot, ...], inputs: FactorInputs, identity: str, evaluated_at: datetime
) -> dict[str, object]:
    """Report production eligibility separately from pure computation throughput."""
    started = time.monotonic()
    plans = build_candidate_plans(
        features,
        None,
        CandidatePlanningContext(
            evaluated_at,
            identity,
            inputs.policy,
            inputs.models,
            120,
        ),
    )
    planning_ms = _elapsed(started)
    results = []
    for strategy, constructor in ((Strategy.TOMORROW, TomorrowNativeInput), (Strategy.D25, D25NativeInput)):
        codes = plans.strategy_codes(strategy)
        candidate_codes = set(codes)
        candidates = tuple(item for item in features if item.quote.code in candidate_codes)
        started = time.monotonic()
        diagnostic_candidates = tuple(item for item in features if inputs.models.is_input_eligible(strategy, item))[
            :360
        ]
        model = inputs.models.score(strategy, diagnostic_candidates)
        inference_ms = _elapsed(started)
        started = time.monotonic()
        local_scores = tuple(
            score_board_strategy(item, policy).base_score
            for item in diagnostic_candidates
            if (policy := inputs.policy.board_policy(strategy, item.quote.board)) is not None
        )
        local_math_ms = _elapsed(started)
        started = time.monotonic()
        detail: dict[str, object] = {}
        if features:
            native = constructor(
                evaluated_at.date(),
                "factor_diagnostic",
                identity,
                inputs.config_version,
                evaluated_at,
                features,
                codes,
                candidates,
                30.0,
                30.0,
                120,
            )
            try:
                projection = build_scored_local(
                    native, inputs.policy, sequence=1, runtime=ScoredProjectionInputs(model_scoring=inputs.models)
                )
                detail = {
                    "state": "constructed",
                    "item_count": len(projection.local.items),
                    "pipeline_stage_count": len(projection.local.pipeline.stage_snapshots)
                    if projection.local.pipeline is not None
                    else 0,
                    "local_score_output_count": len(projection.local.items),
                }
            except (RuntimeError, ValueError, TypeError) as exc:
                detail = {"state": "failed", "error": type(exc).__name__}
        else:
            detail = {"state": "not_ready", "reason": "no_market_features"}
        results.append(
            {
                "strategy": strategy.value,
                "candidate_count": len(candidates),
                "diagnostic_model_input_count": len(diagnostic_candidates),
                "diagnostic_model_output_count": len(model.predictions) if model else 0,
                "diagnostic_local_score_count": len(local_scores),
                "diagnostic_inference_ms": inference_ms,
                "diagnostic_local_math_ms": local_math_ms,
                "production_projection_ms": _elapsed(started),
                "projection": detail,
                "note": "Model cache warmed by diagnostic inference; local math is pre-risk base score; production projection retains freshness and risk gates",
            }
        )
    return {"candidate_planning_ms": planning_ms, "strategies": results}


def run_sample(
    sources: tuple[str, ...],
    codes: tuple[str, ...],
    inputs: FactorInputs,
    references: FixedReferences,
    official: tuple[SourceObservation, ...],
    batch: CollectedBatch,
    args: argparse.Namespace,
    number: int,
    size: int,
    auxiliary: AuxiliaryInputs,
) -> dict[str, object]:
    started = time.monotonic()
    attempts = batch.attempts
    missing = batch.requested_gaps
    received_at = batch.received_at
    acquisition_ms = batch.acquisition_ms
    identity = f"factor_comparison:{inputs.profile}:{size}:{number}:{'+'.join(sources)}"
    allowed = set(codes)
    observations = tuple(
        observation_from_quote(quote, source=quote.source, observed_at=received_at)
        for attempt in attempts
        for quote in attempt.quotes
    )
    selected_references = tuple(item for item in (*references.observations, *official) if item.subject_key in allowed)
    merge_started = time.monotonic()
    merged = merge_market_observations(
        (*observations, *selected_references), observed_at=received_at, merge_epoch=identity
    )
    # Reference-only rows must not masquerade as returned supplier quotations.
    quote_codes = {quote.code for attempt in attempts for quote in attempt.quotes}
    quotes = tuple(quote for quote in merged.quotes if quote.code in quote_codes)
    merge_ms = _elapsed(merge_started)
    feature_started = time.monotonic()
    features = inputs.builder.build(
        quotes,
        inputs.histories,
        received_at,
        history_summaries=inputs.contexts,
        research_observations=auxiliary.research,
        intraday_minutes=auxiliary.minutes,
    )
    features = attach_model_industries(features, references)
    feature_ms = _elapsed(feature_started)
    # Each profile replays the same input cut; the previous profile's execution
    # time must not age the next profile's source timestamps artificially.
    evaluated_at = received_at + timedelta(milliseconds=_elapsed(started))
    audit = factor_audit(features, inputs, codes)
    seal_ms = acquisition_ms + _elapsed(started)
    evaluated_at = received_at + timedelta(milliseconds=_elapsed(started))
    scoring_started = time.monotonic()
    try:
        scoring = _score(features, inputs, identity, evaluated_at)
    except (RuntimeError, ValueError, TypeError) as exc:
        scoring = {"state": "failed", "error": type(exc).__name__}
    scoring_ms = _elapsed(scoring_started)
    audit_quote = quote_audit(quotes, codes, evaluated_at)
    after_close = evaluated_at.date() not in references.open_dates or not (
        "09:30" <= evaluated_at.strftime("%H:%M") < "11:30" or "13:00" <= evaluated_at.strftime("%H:%M") < "15:00"
    )
    return {
        "profile": inputs.profile,
        "scope": size or "full_market",
        "round": number,
        "input_cut": received_at.isoformat(),
        "input_quote_hash": hashlib.sha256(
            repr(tuple(quote for attempt in attempts for quote in attempt.quotes)).encode()
        ).hexdigest(),
        "requested_population_hash": hashlib.sha256("|".join(codes).encode()).hexdigest(),
        "route": "+".join(sources),
        "collected_at": evaluated_at.isoformat(),
        "market_session": "outside_continuous_trading" if after_close else "continuous_trading",
        "acquisition_ms": acquisition_ms,
        "merge_ms": merge_ms,
        "feature_build_ms": feature_ms,
        "input_seal_ms": seal_ms,
        "scoring_ms": scoring_ms,
        "total_ms": acquisition_ms + _elapsed(started),
        "raw_quote_complete": audit_quote["core_complete"] == len(codes),
        "factor_complete": audit["required_factors_complete"],
        "input_qualified": audit["required_factors_complete"] is True
        and audit_quote["core_complete"] == len(codes)
        and audit_quote["fresh_source_timestamp_rows"] == len(codes)
        and seal_ms <= args.batch_seconds * 1000
        and not after_close,
        "deadline_exceeded": seal_ms > args.batch_seconds * 1000,
        "attempts": [
            _attempt_report(item, codes if index == 0 else missing, received_at) for index, item in enumerate(attempts)
        ],
        "quotes": audit_quote,
        "factors": audit,
        "scoring": scoring,
        "merge_conflict_count": len(merged.conflicts),
        "field_source_counts": dict(
            Counter(source for fields in merged.field_sources.values() for source in fields.values())
        ),
        "status": "measured" if quotes else "source_failed",
        "published": False,
    }


def summarize(samples: list[dict[str, object]]) -> list[dict[str, object]]:
    groups: dict[tuple[object, ...], list[dict[str, object]]] = {}
    for sample in samples:
        groups.setdefault((sample["profile"], sample["scope"], sample["route"]), []).append(sample)
    return [
        {
            "profile": key[0],
            "scope": key[1],
            "route": key[2],
            "rounds": len(rows),
            "measured_rounds": sum(row.get("status") == "measured" for row in rows),
            "deadline_exceeded_rounds": sum(row.get("deadline_exceeded") is True for row in rows),
            "raw_quote_complete_rounds": sum(row.get("raw_quote_complete") is True for row in rows),
            "factor_complete_rounds": sum(row.get("factor_complete") is True for row in rows),
            "complete_input_timings_ms": summarize_latency_ms(
                [float(str(row["input_seal_ms"])) for row in rows if row.get("input_qualified") is True]
            ),
            "timings_ms": {
                name: summarize_latency_ms([float(str(row[name])) for row in rows if name in row])
                for name in (
                    "acquisition_ms",
                    "merge_ms",
                    "feature_build_ms",
                    "input_seal_ms",
                    "scoring_ms",
                    "total_ms",
                )
            },
        }
        for key, rows in groups.items()
    ]


def collect(args: argparse.Namespace) -> dict[str, object]:
    settings = load_runtime_settings(args.config)
    at = _now()
    started = time.monotonic()
    references = read_references(settings.runtime_dir / "profiles" / args.profiles[0], at)
    industry_started = time.monotonic()
    industry_error = None
    try:
        industry_observations = BaoStockIndustryClient(timeout_seconds=15).fetch(at)
        industries = {
            item.subject_key: ModelIndustryReference(
                str(item.fields["model_industry"]),
                str(item.fields["model_industry_classification"]),
                datetime.fromisoformat(str(item.fields["model_industry_effective_date"])).date(),
                str(item.fields["model_industry_source"]),
                str(item.fields["model_industry_data_version"]),
            )
            for item in industry_observations
        }
        references = replace(
            references,
            model_industries=industries,
            observations=(*references.observations, *industry_observations),
            versions=(*references.versions, *(sorted({item.data_version for item in industry_observations}))),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        industry_error = type(exc).__name__
    industry_ms = _elapsed(industry_started)
    client = ExchangeSecurityMasterClient(
        sse_fetcher=partial(fetch_sse_listings, get=requests.get),
        szse_fetcher=partial(fetch_szse_listings, get=requests.get),
        timeout_seconds=15.0,
        minimum_rows=4000,
        wall_clock=_now,
    )
    official = client.fetch(at)
    reference_tencent = TencentClient(
        timeout_seconds=args.timeout_seconds, session_factory=requests.Session, worker_pool=None
    )
    reference_gateway = MarketDataGateway(
        TencentFullMarketSource(reference_tencent, lambda: tuple(item.subject_key for item in official)),
        SinaClient(timeout_seconds=args.timeout_seconds, session_factory=requests.Session, worker_pool=None),
        reference_tencent,
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=1,
        listing_open_dates=lambda: references.open_dates,
    )
    reference_gateway.update_reference_observations((*references.observations, *official))
    official = tuple(reference_gateway.reference_observations(tuple(item.subject_key for item in official)))
    setup_ms = _elapsed(started)
    all_codes = select_codes(official, 0)
    profiles = []
    preparation = []
    for profile in args.profiles:
        try:
            inputs = load_factor_inputs(args.config, args.qfq_root, profile, all_codes, at)
            profiles.append(inputs)
            preparation.append(
                {
                    "profile": profile,
                    "qfq_snapshot": inputs.snapshot_hash,
                    "config_version": inputs.config_version,
                    "timings_ms": dict(inputs.preparation_ms),
                    "status": "ready",
                }
            )
        except (OSError, RuntimeError, ValueError) as exc:
            preparation.append({"profile": profile, "status": "failed", "error": type(exc).__name__})
    pool = BoundedExecutor(worker_count=6, queue_capacity=12, thread_name_prefix="factor-comparison")
    pool.start()
    samples: list[dict[str, object]] = []
    routes: list[tuple[str, ...]] = [(source,) for source in args.sources] if args.mode != "combined" else []
    if args.mode != "single" and "tencent" in args.sources:
        routes.extend(("tencent", source) for source in ("sina", "eastmoney") if source in args.sources)
    try:
        auxiliary = probe_auxiliary(
            select_codes(official, 3),
            _now(),
            load_strategy_settings(settings.strategy_config_path).long_research,
            args.timeout_seconds,
            pool,
        )
        for size in args.sizes:
            codes = select_codes(official, size)
            for number in range(1, args.rounds + 1):
                rotated = routes[number % len(routes) :] + routes[: number % len(routes)]
                for route in rotated:
                    batch = acquire_batch(route, codes, pool, args)
                    for inputs in profiles:
                        sample_started = time.monotonic()
                        try:
                            sample = run_sample(
                                route, codes, inputs, references, official, batch, args, number, size, auxiliary
                            )
                        except (OSError, RuntimeError, ValueError, TypeError) as exc:
                            sample = {
                                "profile": inputs.profile,
                                "scope": size or "full_market",
                                "round": number,
                                "route": "+".join(route),
                                "status": "failed",
                                "error": type(exc).__name__,
                                "acquisition_ms": batch.acquisition_ms,
                                "total_ms": batch.acquisition_ms + _elapsed(sample_started),
                                "attempts": [
                                    _attempt_report(item, codes, batch.received_at) for item in batch.attempts
                                ],
                            }
                        samples.append(sample)
                    if args.interval_seconds:
                        time.sleep(args.interval_seconds)
    finally:
        pool.stop(wait=True, cancel_futures=True)
    return {
        "schema_version": "market-factor-comparison",
        "status": "degraded" if any(sample["status"] == "measured" for sample in samples) else "failed",
        "summary": {
            "official_population": len(all_codes),
            "reference_rows": len(references.observations),
            "reference_profile": args.profiles[0],
            "model_industry_probe": {
                "source": "baostock",
                "rows": len(references.model_industries),
                "error": industry_error,
                "elapsed_ms": industry_ms,
            },
            "reference_setup_ms": setup_ms,
            "reference_versions": list(references.versions),
            "preparation": preparation,
            "auxiliary_samples": list(auxiliary.report),
            "comparisons": summarize(samples),
            "samples": samples,
            "production_switch": False,
            "production_publication": False,
            "limitations": [
                "After-close probes cannot establish intraday freshness",
                "Research and minute probes cover only three board samples; no full-population completeness claim",
                "No browser/SSE timing; result construction only",
                "total_ms includes diagnostic field audit and throughput probes; it is not production publication latency",
                "Official quote population includes stocks outside the admitted qfq population; uncovered histories are not all download failures",
                "V2/V3 replay identical quotes at the acquisition cut plus their own computation duration",
                "120/720 scopes measure transport and computation; board ranking requires the full eligible population",
                "Source failures and missing inputs cannot be counted as fast complete rounds",
                "Eastmoney spot is diagnostic-only, bounded to 64 pages and six workers; production has retired its market lane",
            ],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--qfq-root", type=Path, required=True)
    parser.add_argument("--sources", nargs="+", choices=_SOURCES, default=list(_SOURCES))
    parser.add_argument("--profiles", nargs="+", choices=("v2", "v3"), default=["v2", "v3"])
    parser.add_argument("--sizes", nargs="+", type=int, default=[120, 720, 0])
    parser.add_argument("--mode", choices=("single", "combined", "all"), default="all")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--interval-seconds", type=float, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=5)
    parser.add_argument("--batch-seconds", type=float, default=30)
    args = parser.parse_args()
    if (
        not 1 <= args.rounds <= 100
        or len(args.sizes) > 3
        or any(size not in (0, 120, 720) for size in args.sizes)
        or not all(math.isfinite(value) for value in (args.interval_seconds, args.timeout_seconds, args.batch_seconds))
        or not 0 <= args.interval_seconds <= 60
        or not 0 < args.timeout_seconds <= 30
        or not 0 < args.batch_seconds <= 120
        or len(set(args.sources)) != len(args.sources)
        or len(set(args.profiles)) != len(args.profiles)
        or len(set(args.sizes)) != len(args.sizes)
        or args.mode == "combined"
        and ("tencent" not in args.sources or not set(args.sources).intersection({"sina", "eastmoney"}))
    ):
        parser.error("comparison requires bounded distinct sources, sizes, rounds and deadlines")
    # SDK subprocesses inherit file descriptors, not Python's sys.stdout.
    # Keep their login messages away from the single JSON result protocol.
    sys.stdout.flush()
    result_descriptor = os.dup(1)
    try:
        os.dup2(2, 1)
        try:
            with redirect_stdout(sys.stderr):
                report = collect(args)
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            report = {
                "schema_version": "market-factor-comparison",
                "status": "failed",
                "summary": {"error": type(exc).__name__},
            }
    finally:
        os.dup2(result_descriptor, 1)
        os.close(result_descriptor)
    emit_report(report)
    return int(report["status"] == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
