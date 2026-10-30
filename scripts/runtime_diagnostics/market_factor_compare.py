"""Bounded live quote comparisons with fixed qfq inputs and isolated production scoring."""

from __future__ import annotations

import argparse
import cProfile
import hashlib
import math
import os
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from trader.bootstrap import CreateCompanyEligibilityReader, FetchExternalHttp, OpenExternalHttp
from trader.download.infra.exchange_security_master import (
    ExchangeSecurityMasterClient,
    FetchSseListings,
    FetchSzseListings,
)
from trader.infra.http_contracts import HttpError
from trader.infra.market_data.observations import SourceObservation
from trader.infra.settings import LoadRuntimeSettings, LoadStrategySettings
from trader.infra.workers import BoundedExecutor
from trader.recommendation.application.pipeline.candidate_pool.candidate_builder import (
    BuildCandidatePlans,
    CandidatePlanningContext,
)
from trader.recommendation.application.pipeline.final_selection.freeze_projection import (
    BuildScoredLocal,
    ScoredProjectionInputs,
)
from trader.recommendation.application.ports.scoring import D25NativeInput, TomorrowNativeInput
from trader.recommendation.domain.market.industry import QuoteWithIndustry
from trader.recommendation.domain.market.models import FeatureSnapshot, MarketQuote
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.scoring import ScoreBoardStrategy
from trader.recommendation.infra.market_data.gateway import MarketDataGateway
from trader.recommendation.infra.market_data.providers.sina import SinaClient
from trader.recommendation.infra.market_data.providers.tencent import TencentClient, TencentFullMarketSource
from trader.recommendation.infra.normalization.merge import MergeMarketObservations, ObservationFromQuote

from .market_factor_audit import (
    DERIVABLE_QUOTE_FIELDS,
    IGNORED_REALTIME_FIELDS,
    OPTIONAL_FALLBACK_QUOTE_FIELDS,
    QUOTE_FIELDS,
    REQUIRED_FREEZE_QUOTE_FIELDS,
    SCORING_QUOTE_FIELDS,
    VALIDATION_QUOTE_FIELDS,
    FactorAudit,
    MissingQuoteCodes,
    QuoteAudit,
    QuoteFieldsComplete,
)
from .market_factor_auxiliary import AuxiliaryInputs, ProbeAuxiliary
from .market_factor_baostock import BaostockCapability, BaoStockSourceResults, ProbeBaoStockIndustry
from .market_factor_budget import (
    MARKET_SOURCE_LOOP_LIMIT_SECONDS,
    MARKET_TASK_LIMIT_SECONDS,
    DiagnosticBudget,
)
from .market_factor_eastmoney import FetchEastmoneyQuotes
from .market_factor_inputs import (
    AttachModelIndustries,
    FactorInputs,
    FixedReferences,
    LoadFactorInputs,
    ReadReferences,
)
from .market_factor_routes import RoutePlan, RoutePlans
from .reporting import emit_report, summarize_latency_ms

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SOURCES = ("tencent", "eastmoney", "sina", "baostock")
_QUOTE_SOURCES = frozenset({"tencent", "eastmoney", "sina"})
_SOURCE_WORKERS = {"tencent": 3, "eastmoney": 6, "sina": 5}


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
    requested_codes: tuple[tuple[str, ...], ...]
    received_at: datetime
    acquisition_ms: float


def AcquireBatch(
    plan: RoutePlan,
    codes: tuple[str, ...],
    pools: Mapping[str, BoundedExecutor],
    args: argparse.Namespace,
    budget: DiagnosticBudget,
) -> CollectedBatch:
    started = time.monotonic()
    deadline = min(started + args.batch_seconds, budget.task_deadline())
    dynamic_sources = tuple(source for source in plan.sources if source in _QUOTE_SOURCES)
    if not dynamic_sources:
        return CollectedBatch((), (), _now(), _elapsed(started))
    primary = dynamic_sources[0]
    primary_deadline = min(deadline, started + 12.0, started + budget.source_remaining_seconds(primary))
    first = FetchSource(primary, codes, pools[primary], args.timeout_seconds, primary_deadline)
    attempts = [first]
    requests = [codes]
    if (
        plan.scheduling == "sequential_failure_backup"
        and (first.error is not None or MissingQuoteCodes(first.quotes, codes, _now()))
        and time.monotonic() < deadline
    ):
        auxiliary = dynamic_sources[1]
        backup_deadline = min(deadline, time.monotonic() + budget.source_remaining_seconds(auxiliary))
        attempts.append(FetchSource(auxiliary, codes, pools[auxiliary], args.timeout_seconds, backup_deadline))
        requests.append(codes)
    return CollectedBatch(tuple(attempts), tuple(requests), _now(), _elapsed(started))


def _now() -> datetime:
    return datetime.now(_SHANGHAI)


def _elapsed(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 3)


def SelectCodes(official: Sequence[SourceObservation], size: int) -> tuple[str, ...]:
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


def FetchSource(
    source: str, codes: tuple[str, ...], pool: BoundedExecutor, timeout: float, deadline: float
) -> SourceAttempt:
    started = time.monotonic()
    remaining = deadline - started
    if remaining <= 0:
        return SourceAttempt(source, (), 0.0, 0, "batch_deadline")
    end_at = _now() + timedelta(seconds=remaining)
    try:
        if source == "tencent":
            quotes = TencentClient(
                timeout_seconds=min(timeout, remaining), session_factory=OpenExternalHttp, worker_pool=pool
            ).fetch_quotes(codes, deadline=end_at)
        elif source == "eastmoney":
            quotes = FetchEastmoneyQuotes(min(timeout, remaining), deadline, pool)
        elif source == "sina":
            quotes = SinaClient(
                timeout_seconds=min(timeout, remaining), session_factory=OpenExternalHttp, worker_pool=pool
            ).fetch_market(deadline=end_at)
        else:
            raise ValueError("unknown comparison source")
        physical_rows = len(quotes)
        allowed = set(codes)
        quotes = tuple(quote for quote in quotes if quote.code in allowed)
        if time.monotonic() > deadline:
            return SourceAttempt(source, (), _elapsed(started), physical_rows, "batch_deadline")
        return SourceAttempt(source, quotes, _elapsed(started), physical_rows, None)
    except (OSError, RuntimeError, ValueError, HttpError) as exc:
        return SourceAttempt(source, (), _elapsed(started), 0, type(exc).__name__)


def _attempt_report(attempt: SourceAttempt, codes: tuple[str, ...], at: datetime) -> dict[str, object]:
    return {
        "source": attempt.source,
        "elapsed_ms": attempt.elapsed_ms,
        "error": attempt.error,
        "physical_rows": attempt.physical_rows,
        "audit": QuoteAudit(attempt.quotes, codes, at),
    }


def _score(
    features: tuple[FeatureSnapshot, ...], inputs: FactorInputs, identity: str, evaluated_at: datetime
) -> dict[str, object]:
    """Report production eligibility separately from pure computation throughput."""
    started = time.monotonic()
    plans = BuildCandidatePlans(
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
            ScoreBoardStrategy(item, policy).base_score
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
                projection = BuildScoredLocal(
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


def RunSample(
    plan: RoutePlan,
    codes: tuple[str, ...],
    inputs: FactorInputs,
    references: FixedReferences,
    official: tuple[SourceObservation, ...],
    batch: CollectedBatch,
    args: argparse.Namespace,
    number: int,
    size: int,
    auxiliary: AuxiliaryInputs,
    *,
    cpu_profile_dir: Path | None = None,
) -> dict[str, object]:
    started = time.monotonic()
    attempts = batch.attempts
    received_at = batch.received_at
    acquisition_ms = batch.acquisition_ms
    identity = f"factor_comparison:{inputs.profile}:{size}:{number}:{plan.name}"
    allowed = set(codes)
    observations = tuple(
        ObservationFromQuote(quote, source=quote.source, observed_at=received_at)
        for attempt in attempts
        for quote in attempt.quotes
    )
    selected_references = tuple(item for item in (*references.observations, *official) if item.subject_key in allowed)
    merge_started = time.monotonic()
    merged = MergeMarketObservations(
        (*observations, *selected_references), observed_at=received_at, merge_epoch=identity
    )
    # Reference-only rows must not masquerade as returned supplier quotations.
    quote_codes = {quote.code for attempt in attempts for quote in attempt.quotes}
    quotes = tuple(quote for quote in merged.quotes if quote.code in quote_codes)
    merge_ms = _elapsed(merge_started)
    validation_started = time.monotonic()
    evaluated_at = received_at + timedelta(milliseconds=_elapsed(started))
    audit_quote = QuoteAudit(quotes, codes, evaluated_at)
    validation_ms = _elapsed(validation_started)
    market_data_ready_ms = acquisition_ms + _elapsed(started)
    feature_started = time.monotonic()
    local_quotes = tuple(
        QuoteWithIndustry(quote, references.model_industries.get(quote.code), received_at.date()) for quote in quotes
    )
    profiler = cProfile.Profile() if cpu_profile_dir is not None else None
    if profiler is not None:
        profiler.enable()
    try:
        features = inputs.builder.build(
            local_quotes,
            inputs.histories,
            received_at,
            history_summaries=inputs.contexts,
            research_observations=auxiliary.research,
            intraday_minutes=auxiliary.minutes,
        )
    finally:
        if profiler is not None and cpu_profile_dir is not None:
            profiler.disable()
            profiler.dump_stats(cpu_profile_dir / f"features-{inputs.profile}-{size}-{number}-{plan.name}.pstats")
    features = AttachModelIndustries(features, references)
    feature_ms = _elapsed(feature_started)
    audit = FactorAudit(features, inputs, codes)
    seal_ms = acquisition_ms + _elapsed(started)
    evaluated_at = received_at + timedelta(milliseconds=_elapsed(started))
    scoring_started = time.monotonic()
    try:
        scoring = _score(features, inputs, identity, evaluated_at)
    except (RuntimeError, ValueError, TypeError) as exc:
        scoring = {"state": "failed", "error": type(exc).__name__}
    scoring_ms = _elapsed(scoring_started)
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
        "route": plan.name,
        "scheduling": plan.scheduling,
        "sources": list(plan.sources),
        "collected_at": evaluated_at.isoformat(),
        "market_session": "outside_continuous_trading" if after_close else "continuous_trading",
        "acquisition_ms": acquisition_ms,
        "merge_ms": merge_ms,
        "market_validation_ms": validation_ms,
        "market_data_ready_ms": market_data_ready_ms,
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
            _attempt_report(item, requested, received_at)
            for item, requested in zip(attempts, batch.requested_codes, strict=True)
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


def Summarize(samples: list[dict[str, object]]) -> list[dict[str, object]]:
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
                    "market_validation_ms",
                    "market_data_ready_ms",
                    "feature_build_ms",
                    "input_seal_ms",
                    "scoring_ms",
                    "total_ms",
                )
            },
        }
        for key, rows in groups.items()
    ]


def _field_availability(audits: Sequence[Mapping[str, object]]) -> dict[str, object]:
    coverage: dict[str, list[float]] = {field: [] for field in QUOTE_FIELDS}
    for audit in audits:
        fields = audit.get("fields")
        if not isinstance(fields, list):
            continue
        for row in fields:
            if not isinstance(row, dict) or row.get("field") not in coverage:
                continue
            value = row.get("coverage")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                coverage[str(row["field"])].append(float(value))
    complete = [field for field, values in coverage.items() if values and min(values) == 1.0]
    unavailable = [field for field, values in coverage.items() if values and max(values) == 0.0]
    partial = [
        {
            "field": field,
            "minimum_coverage": round(min(values), 4),
            "maximum_coverage": round(max(values), 4),
        }
        for field, values in coverage.items()
        if values and field not in complete and field not in unavailable
    ]
    return {
        "complete_fields": complete,
        "partial_fields": partial,
        "unavailable_fields": unavailable,
        "unmeasured_fields": [field for field, values in coverage.items() if not values],
    }


def SummarizeSources(acquisitions: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    groups: dict[tuple[object, object], list[Mapping[str, object]]] = {}
    for acquisition in acquisitions:
        if acquisition.get("scheduling") != "single":
            continue
        attempts = acquisition.get("attempts")
        if not isinstance(attempts, list) or not attempts or not isinstance(attempts[0], dict):
            continue
        attempt = attempts[0]
        groups.setdefault((acquisition.get("scope"), attempt.get("source")), []).append(attempt)
    results: list[dict[str, object]] = []
    for (scope, source), attempts in groups.items():
        successful = [attempt for attempt in attempts if attempt.get("error") is None]
        audits = [
            cast(Mapping[str, object], attempt["audit"])
            for attempt in successful
            if isinstance(attempt.get("audit"), dict)
        ]
        results.append(
            {
                "source": source,
                "scope": scope,
                "rounds": len(attempts),
                "successful_rounds": len(successful),
                "failed_rounds": len(attempts) - len(successful),
                "errors": dict(Counter(str(item["error"]) for item in attempts if item.get("error") is not None)),
                "request_timings_ms": summarize_latency_ms(
                    [float(str(item["elapsed_ms"])) for item in attempts if "elapsed_ms" in item]
                ),
                "returned_rows": summarize_latency_ms([float(str(audit.get("returned", 0))) for audit in audits]),
                "physical_rows": summarize_latency_ms(
                    [float(str(item.get("physical_rows", 0))) for item in successful]
                ),
                "source_time_span_seconds": summarize_latency_ms(
                    [
                        float(str(audit["source_time_span_seconds"]))
                        for audit in audits
                        if isinstance(audit.get("source_time_span_seconds"), (int, float))
                    ]
                ),
                "core_complete_rounds": sum(audit.get("core_complete") == audit.get("requested") for audit in audits),
                "fields": _field_availability(audits),
            }
        )
    return sorted(results, key=lambda item: (str(item["scope"]), str(item["source"])))


def SummarizeRoutes(samples: Sequence[Mapping[str, object]], profile: str) -> list[dict[str, object]]:
    groups: dict[tuple[object, object], list[Mapping[str, object]]] = {}
    for sample in samples:
        if sample.get("profile") == profile:
            groups.setdefault((sample.get("scope"), sample.get("route")), []).append(sample)
    results: list[dict[str, object]] = []
    for (scope, route), rows in groups.items():
        measured = [row for row in rows if row.get("status") == "measured"]
        audits = [cast(Mapping[str, object], row["quotes"]) for row in measured if isinstance(row.get("quotes"), dict)]
        fields = _field_availability(audits)
        factor_audits = [
            cast(Mapping[str, object], row["factors"]) for row in measured if isinstance(row.get("factors"), dict)
        ]
        all_fields_complete_rounds = sum(QuoteFieldsComplete(audit, QUOTE_FIELDS) for audit in audits)
        scoring_fields_complete_rounds = sum(QuoteFieldsComplete(audit, SCORING_QUOTE_FIELDS) for audit in audits)
        selection_fields_complete_rounds = sum(
            QuoteFieldsComplete(audit, REQUIRED_FREEZE_QUOTE_FIELDS) for audit in audits
        )
        model_industry_complete_rounds = sum(
            int(str(factors.get("model_industry_rows", 0))) == int(str(audit.get("requested", -1)))
            for factors, audit in zip(factor_audits, audits, strict=False)
        )
        requested_population = max((int(str(audit.get("requested", 0))) for audit in audits), default=0)
        model_industry_rows_best = max(
            (int(str(item.get("model_industry_rows", 0))) for item in factor_audits), default=0
        )
        results.append(
            {
                "route": route,
                "scheduling": rows[0].get("scheduling"),
                "sources": rows[0].get("sources"),
                "scope": scope,
                "rounds": len(rows),
                "measured_rounds": len(measured),
                "core_complete_rounds": sum(row.get("raw_quote_complete") is True for row in measured),
                "all_market_fields_complete_rounds": all_fields_complete_rounds,
                "scoring_market_fields_complete_rounds": scoring_fields_complete_rounds,
                "selection_market_fields_complete_rounds": selection_fields_complete_rounds,
                "model_industry_complete_rounds": model_industry_complete_rounds,
                "source_owned_inputs_complete_rounds": sum(
                    isinstance(row.get("quotes"), dict)
                    and QuoteFieldsComplete(cast(Mapping[str, object], row["quotes"]), REQUIRED_FREEZE_QUOTE_FIELDS)
                    and isinstance(row.get("factors"), dict)
                    and int(str(cast(Mapping[str, object], row["factors"]).get("model_industry_rows", 0)))
                    == int(str(cast(Mapping[str, object], row["quotes"]).get("requested", -1)))
                    for row in measured
                    if isinstance(row.get("quotes"), dict)
                ),
                "complete_scoring_input_rounds": sum(row.get("input_qualified") is True for row in measured),
                "continuous_trading_rounds": sum(row.get("market_session") == "continuous_trading" for row in measured),
                "complete_scoring_input_timings_ms": summarize_latency_ms(
                    [float(str(row["input_seal_ms"])) for row in measured if row.get("input_qualified") is True]
                ),
                "model_industry_rows_best": model_industry_rows_best,
                "model_industry_coverage_best": (
                    round(model_industry_rows_best / requested_population, 4) if requested_population else 0.0
                ),
                "fields": fields,
                "timings_ms": {
                    name: summarize_latency_ms([float(str(row[name])) for row in measured if name in row])
                    for name in (
                        "acquisition_ms",
                        "merge_ms",
                        "market_validation_ms",
                        "market_data_ready_ms",
                        "input_seal_ms",
                        "scoring_ms",
                        "total_ms",
                    )
                },
                "source_time_span_seconds": summarize_latency_ms(
                    [
                        float(str(audit["source_time_span_seconds"]))
                        for audit in audits
                        if isinstance(audit.get("source_time_span_seconds"), (int, float))
                    ]
                ),
            }
        )
    return sorted(results, key=lambda item: (str(item["scope"]), str(item["route"])))


def _recommended_route(
    routes: Sequence[Mapping[str, object]], excluded_sources: frozenset[str] = frozenset()
) -> Mapping[str, object] | None:
    candidates = [
        route
        for route in routes
        if route.get("measured_rounds")
        and isinstance(route.get("sources"), list)
        and cast(list[object], route["sources"])
        and cast(list[object], route["sources"])[0] == "tencent"
        and not excluded_sources.intersection(str(source) for source in cast(list[object], route["sources"]))
    ]
    if not candidates:
        return None
    full_market = [route for route in candidates if route.get("scope") == "full_market"]
    if full_market:
        candidates = full_market

    def rank(route: Mapping[str, object]) -> tuple[float, float, float, float, float, float, float, float, float]:
        rounds = max(1, int(str(route.get("rounds", 1))))
        fields: Mapping[str, object] = (
            cast(Mapping[str, object], route["fields"]) if isinstance(route.get("fields"), dict) else {}
        )
        complete = (
            cast(list[object], fields["complete_fields"]) if isinstance(fields.get("complete_fields"), list) else []
        )
        timings: Mapping[str, object] = (
            cast(Mapping[str, object], route["timings_ms"]) if isinstance(route.get("timings_ms"), dict) else {}
        )
        market_ready: Mapping[str, object] = (
            cast(Mapping[str, object], timings["market_data_ready_ms"])
            if isinstance(timings.get("market_data_ready_ms"), dict)
            else {}
        )
        p95 = market_ready.get("p95_ms")
        latency = float(p95) if isinstance(p95, (int, float)) else math.inf
        sources = route.get("sources")
        source_count = len(sources) if isinstance(sources, list) else len(_SOURCES)
        return (
            float(str(route.get("source_owned_inputs_complete_rounds", 0))) / rounds,
            float(str(route.get("core_complete_rounds", 0))) / rounds,
            float(str(route.get("model_industry_complete_rounds", 0))) / rounds,
            float(str(route.get("model_industry_coverage_best", 0))),
            float(str(route.get("selection_market_fields_complete_rounds", 0))) / rounds,
            float(str(route.get("scoring_market_fields_complete_rounds", 0))) / rounds,
            float(len(complete)),
            -latency,
            -float(source_count),
        )

    return max(candidates, key=rank)


def SummarizeScoringFields(
    samples: Sequence[Mapping[str, object]], profiles: tuple[str, ...]
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for profile in profiles:
        factors = next(
            (
                cast(Mapping[str, object], row["factors"])
                for row in samples
                if row.get("profile") == profile and isinstance(row.get("factors"), dict)
            ),
            {},
        )
        owners: dict[str, set[str]] = {}
        fields = factors.get("fields")
        if isinstance(fields, list):
            for field in fields:
                if not isinstance(field, dict) or not field.get("consumers"):
                    continue
                owners.setdefault(str(field.get("input_family")), set()).add(str(field.get("field")))
        model_features: set[str] = set()
        models = factors.get("models")
        if isinstance(models, list):
            for model in models:
                if not isinstance(model, dict) or not isinstance(model.get("feature_contract"), list):
                    continue
                for feature in model["feature_contract"]:
                    if isinstance(feature, dict) and isinstance(feature.get("field"), str):
                        model_features.add(feature["field"])
        results.append(
            {
                "profile": profile,
                "freeze_fields_by_owner": {owner: sorted(names) for owner, names in sorted(owners.items())},
                "model_gate_features": sorted(model_features),
            }
        )
    return results


def SummarizeScoringProfiles(
    samples: Sequence[Mapping[str, object]], selected_route: str | None, profiles: tuple[str, ...]
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for profile in profiles:
        rows = [
            row
            for row in samples
            if row.get("profile") == profile and row.get("route") == selected_route and row.get("status") == "measured"
        ]
        factor_rows = [
            cast(Mapping[str, object], row["factors"]) for row in rows if isinstance(row.get("factors"), dict)
        ]
        missing_by_owner: dict[str, set[str]] = {"intraday_minutes": set(), "research_reference": set()}
        market_qfq_gap_count = 0
        for factors in factor_rows:
            fields = factors.get("fields")
            if not isinstance(fields, list):
                continue
            for field in fields:
                if not isinstance(field, dict) or not field.get("consumers") or not field.get("missing"):
                    continue
                owner = field.get("input_family")
                if owner in missing_by_owner:
                    missing_by_owner[str(owner)].add(str(field.get("field")))
                elif owner == "qfq_and_quote_or_cross_section":
                    market_qfq_gap_count += 1
        models: dict[str, int] = {}
        model_reasons: dict[str, dict[str, int]] = {}
        for factors in factor_rows:
            values = factors.get("models")
            if not isinstance(values, list):
                continue
            for model in values:
                if isinstance(model, dict) and isinstance(model.get("strategy"), str):
                    eligible = int(str(model.get("input_eligible", 0)))
                    best = models.get(model["strategy"], -1)
                    models[model["strategy"]] = max(models.get(model["strategy"], 0), eligible)
                    reasons = model.get("input_reason_counts")
                    if isinstance(reasons, dict) and eligible > best:
                        model_reasons[model["strategy"]] = {
                            str(reason): int(str(count)) for reason, count in reasons.items()
                        }
        results.append(
            {
                "profile": profile,
                "measured_rounds": len(rows),
                "qfq": {
                    "owner": "local_sqlite",
                    "excluded_from_market_source_comparison": True,
                    "required_sessions": factor_rows[0].get("qfq_required_sessions") if factor_rows else None,
                    "complete_rows_best": max(
                        (int(str(factors.get("qfq_complete_rows", 0))) for factors in factor_rows), default=0
                    ),
                    "missing_rows": factor_rows[0].get("qfq_missing_rows") if factor_rows else None,
                    "missing_codes": factor_rows[0].get("qfq_missing_codes") if factor_rows else None,
                    "one_session_short_rows": factor_rows[0].get("qfq_one_session_short_rows") if factor_rows else None,
                    "length_distribution": factor_rows[0].get("qfq_length_distribution") if factor_rows else None,
                    "short_windows": factor_rows[0].get("qfq_short_windows") if factor_rows else None,
                },
                "independent_missing_fields": {owner: sorted(fields) for owner, fields in missing_by_owner.items()},
                "market_or_qfq_factor_gap_occurrences": market_qfq_gap_count,
                "model_input_eligible_best": models,
                "model_input_reason_counts": model_reasons,
            }
        )
    return results


def CompactSummary(
    *,
    samples: list[dict[str, object]],
    acquisitions: list[dict[str, object]],
    profiles: tuple[str, ...],
    official_population: int,
    eligible_population: int,
    eligibility: dict[str, object],
    compared_sources: tuple[str, ...],
    baostock_results: list[dict[str, object]],
    preparation: list[dict[str, object]],
    reference_setup_ms: float,
    industry_probe: dict[str, object],
    auxiliary_report: Sequence[Mapping[str, object]],
    budget: dict[str, object],
) -> dict[str, object]:
    sources = SummarizeSources(acquisitions)
    sources.extend(baostock_results)
    routes = SummarizeRoutes(samples, profiles[0])
    single_route_timings = {
        (route.get("scope"), route.get("route")): route.get("timings_ms")
        for route in routes
        if route.get("scheduling") == "single"
    }
    for source in sources:
        timings = single_route_timings.get((source.get("scope"), source.get("source")))
        if isinstance(timings, dict):
            source["data_ready_timings_ms"] = timings.get("market_data_ready_ms")
    excluded_rows = budget.get("excluded_sources")
    excluded_sources = (
        frozenset(str(item.get("source")) for item in excluded_rows if isinstance(item, dict))
        if isinstance(excluded_rows, list)
        else frozenset()
    )
    selected = _recommended_route(routes, excluded_sources)
    selected_route = str(selected["route"]) if selected is not None else None
    selected_fields = selected.get("fields") if selected is not None else {}
    unresolved: list[str] = []
    if isinstance(selected_fields, dict):
        partial = selected_fields.get("partial_fields")
        missing = selected_fields.get("unavailable_fields")
        if isinstance(partial, list):
            unresolved.extend(
                str(item.get("field"))
                for item in partial
                if isinstance(item, dict) and item.get("field") in REQUIRED_FREEZE_QUOTE_FIELDS
            )
        if isinstance(missing, list):
            unresolved.extend(str(item) for item in missing if item in REQUIRED_FREEZE_QUOTE_FIELDS)
    if selected is not None and selected.get("model_industry_complete_rounds") != selected.get("rounds"):
        unresolved.append("model_industry")
    return {
        "official_population": official_population,
        "level_one_eligible_population": eligible_population,
        "level_one_excluded_population": official_population - eligible_population,
        "level_one_eligibility": eligibility,
        "input_owners": {
            "market_sources_compared": list(compared_sources),
            "required_realtime_fields": list(REQUIRED_FREEZE_QUOTE_FIELDS),
            "local_industry_fields": ["industry", "model_industry"],
            "derivable_realtime_fields": list(DERIVABLE_QUOTE_FIELDS),
            "optional_fallback_realtime_fields": list(OPTIONAL_FALLBACK_QUOTE_FIELDS),
            "validation_realtime_fields": list(VALIDATION_QUOTE_FIELDS),
            "ignored_realtime_fields": list(IGNORED_REALTIME_FIELDS),
            "qfq": "local SQLite input; intentionally excluded from supplier field ranking",
            "model_industry": "validated local industry_queries SQLite; BaoStock refresh every seven days",
            "research": "independent financial/announcement/risk evidence",
            "intraday_minutes": "independent candidate minute series",
        },
        "scoring_fields": SummarizeScoringFields(samples, profiles),
        "source_results": sources,
        "route_results": routes,
        "test_budget": budget,
        "recommended_plan": {
            "selected_route": selected_route,
            "scheduling": selected.get("scheduling") if selected is not None else None,
            "sources": selected.get("sources") if selected is not None else None,
            "daily_reference_prepare_ms": industry_probe.get("elapsed_ms", 0.0),
            "all_source_owned_inputs_complete": not unresolved if selected is not None else False,
            "unresolved_source_inputs": sorted(set(unresolved)),
            "execution": [
                "Start local qfq/model/reference preparation and market acquisition in parallel.",
                "Use the selected route and merge immutable observations by field priority and timestamp.",
                "Fetch candidate minute and research evidence independently; do not block a valid market batch on optional evidence.",
                "Publish only after the required input cut is sealed; retain the previous valid snapshot on failure.",
            ],
            "selection_rule": "Tencent full-market primary, Sina only on failure; validated local industry query outside quote routing; maximize complete inputs and successful rounds, then minimize P95 data-ready time",
        },
        "scoring_readiness": SummarizeScoringProfiles(samples, selected_route, profiles),
        "preparation": {
            "reference_setup_ms": reference_setup_ms,
            "profiles": preparation,
            "model_industry": industry_probe,
            "auxiliary_three_code_probe": list(auxiliary_report),
        },
        "limitations": [
            "qfq is read from local SQLite and excluded from market-source field ranking",
            "Research and minute probes cover only three board samples; they do not establish full-market completeness",
            "After-close probes measure coverage and latency but cannot establish intraday freshness",
            "Input-seal and scoring timings do not include API/SSE/Web publication latency",
            "Eastmoney is a diagnostic-only source; the production market lane remains retired",
            "speed and change_5m are not requested by this diagnostic because they do not enter the active candidate filter, local final score, model gate, or missing-input penalty",
            "local CSRC industry has zero numeric score weight but supplies final concentration and model exposure from one validated query",
        ],
    }


def _official_references(
    client: ExchangeSecurityMasterClient,
    observed_at: datetime,
    cached: Sequence[SourceObservation],
) -> tuple[tuple[SourceObservation, ...], str]:
    try:
        return client.fetch(observed_at), "fresh_exchange"
    except (OSError, RuntimeError, ValueError):
        previous = tuple(
            item
            for item in cached
            if item.source == "exchange_security_master"
            and item.source_time <= observed_at
            and item.fields.get("board") in {"main", "chinext", "star"}
            and item.fields.get("listing_date") is not None
        )
        if len({item.subject_key for item in previous}) < 4_000:
            raise
        return previous, "cached_exchange_degraded"


def Collect(args: argparse.Namespace, *, cpu_profile_dir: Path | None = None) -> dict[str, object]:
    budget = DiagnosticBudget(args.source_loop_seconds, args.task_seconds)
    settings = LoadRuntimeSettings(args.config)
    strategy_settings = LoadStrategySettings(settings.strategy_config_path)
    at = _now()
    started = time.monotonic()
    local_references = ReadReferences(settings.data_paths.reference, at)
    base_references = local_references
    baostock_capability: BaostockCapability | None = None
    comparison_references = base_references
    client = ExchangeSecurityMasterClient(
        sse_fetcher=partial(FetchSseListings, get=FetchExternalHttp),
        szse_fetcher=partial(FetchSzseListings, get=FetchExternalHttp),
        timeout_seconds=15.0,
        minimum_rows=4000,
        wall_clock=_now,
    )
    official, official_mode = _official_references(client, at, base_references.observations)
    reference_tencent = TencentClient(
        timeout_seconds=args.timeout_seconds, session_factory=OpenExternalHttp, worker_pool=None
    )
    reference_gateway = MarketDataGateway(
        TencentFullMarketSource(reference_tencent, lambda: tuple(item.subject_key for item in official)),
        SinaClient(timeout_seconds=args.timeout_seconds, session_factory=OpenExternalHttp, worker_pool=None),
        reference_tencent,
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=1,
        listing_open_dates=lambda: base_references.open_dates,
    )
    reference_gateway.update_reference_observations((*base_references.observations, *official))
    official = tuple(reference_gateway.reference_observations(tuple(item.subject_key for item in official)))
    official_codes = SelectCodes(official, 0)
    eligibility_index = CreateCompanyEligibilityReader(
        settings.data_paths.company,
        strategy_settings.company_scoring.events,
    )
    eligibility_status = eligibility_index.status()
    if not eligibility_status.integrity_ok:
        raise RuntimeError("issuer eligibility snapshot integrity failed")
    all_codes = eligibility_index.filter_codes(official_codes, at)
    allowed_codes = set(all_codes)
    official = tuple(item for item in official if item.subject_key in allowed_codes)
    if "baostock" in args.sources:
        baostock_capability = ProbeBaoStockIndustry(
            local_references.model_industries, local_references.industry_read_ms
        )
    setup_ms = _elapsed(started)
    profiles = []
    preparation = []
    input_codes = tuple(dict.fromkeys(code for size in args.sizes for code in SelectCodes(official, size)))
    for profile in args.profiles:
        try:
            inputs = LoadFactorInputs(args.config, args.qfq_database, profile, input_codes, at)
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
    source_pools = {
        source: BoundedExecutor(
            worker_count=_SOURCE_WORKERS[source],
            queue_capacity=_SOURCE_WORKERS[source] * 2,
            thread_name_prefix=f"factor-{source}",
        )
        for source in args.sources
        if source in _QUOTE_SOURCES
    }
    auxiliary_pool = BoundedExecutor(worker_count=6, queue_capacity=12, thread_name_prefix="factor-auxiliary")
    for pool in (*source_pools.values(), auxiliary_pool):
        pool.start()
    samples: list[dict[str, object]] = []
    acquisitions: list[dict[str, object]] = []
    plans = RoutePlans(args.sources, args.mode)
    try:
        auxiliary = ProbeAuxiliary(
            SelectCodes(official, 3),
            _now(),
            strategy_settings.long_research,
            args.timeout_seconds,
            auxiliary_pool,
            company_database=settings.data_paths.company,
            open_dates=local_references.open_dates,
            company_policy=strategy_settings.company_scoring,
        )
        for size in args.sizes:
            codes = SelectCodes(official, size)
            for number in range(1, args.rounds + 1):
                if not plans:
                    continue
                offset = number % len(plans)
                rotated = plans[offset:] + plans[:offset]
                for plan in rotated:
                    if budget.task_expired(plan.sources, plan.name):
                        break
                    if not budget.route_allowed(plan.sources):
                        continue
                    batch = AcquireBatch(plan, codes, source_pools, args, budget)
                    for attempt in batch.attempts:
                        budget.record_source(attempt.source, attempt.elapsed_ms, plan.name)
                    attempts = [
                        _attempt_report(item, requested, batch.received_at)
                        for item, requested in zip(batch.attempts, batch.requested_codes, strict=True)
                    ]
                    acquisitions.append(
                        {
                            "scope": size or "full_market",
                            "round": number,
                            "route": plan.name,
                            "scheduling": plan.scheduling,
                            "sources": list(plan.sources),
                            "acquisition_ms": batch.acquisition_ms,
                            "attempts": attempts,
                        }
                    )
                    if budget.task_expired(plan.sources, plan.name):
                        break
                    if not budget.route_allowed(plan.sources):
                        continue
                    for inputs in profiles:
                        sample_started = time.monotonic()
                        try:
                            sample = RunSample(
                                plan,
                                codes,
                                inputs,
                                comparison_references,
                                official,
                                batch,
                                args,
                                number,
                                size,
                                auxiliary,
                                cpu_profile_dir=cpu_profile_dir,
                            )
                        except (OSError, RuntimeError, ValueError, TypeError) as exc:
                            sample = {
                                "profile": inputs.profile,
                                "scope": size or "full_market",
                                "round": number,
                                "route": plan.name,
                                "scheduling": plan.scheduling,
                                "status": "failed",
                                "error": type(exc).__name__,
                                "acquisition_ms": batch.acquisition_ms,
                                "total_ms": batch.acquisition_ms + _elapsed(sample_started),
                                "attempts": attempts,
                            }
                        samples.append(sample)
                    if budget.task_expired(plan.sources, plan.name):
                        break
                    if args.interval_seconds:
                        time.sleep(args.interval_seconds)
                if budget.task_timed_out:
                    break
            if budget.task_timed_out:
                break
    finally:
        for pool in (*source_pools.values(), auxiliary_pool):
            pool.stop(wait=True, cancel_futures=True)
    industry_probe: dict[str, object] = (
        {
            "source": "baostock",
            "mode": baostock_capability.mode,
            "live_rows": 0,
            "cached_rows": len(baostock_capability.published_industries),
            "published_rows": len(baostock_capability.published_industries),
            "invalid_rows": 0,
            "error": None,
            "elapsed_ms": local_references.industry_read_ms,
        }
        if baostock_capability is not None
        else {
            "source": "local_sqlite",
            "mode": "cached_only",
            "cached_rows": len(local_references.model_industries),
            "published_rows": len(local_references.model_industries),
            "elapsed_ms": local_references.industry_read_ms,
        }
    )
    baostock_results = (
        BaoStockSourceResults(
            baostock_capability,
            tuple((size or "full_market", SelectCodes(official, size)) for size in args.sizes),
            args.rounds,
            QUOTE_FIELDS,
        )
        if baostock_capability is not None
        else []
    )
    summary = CompactSummary(
        samples=samples,
        acquisitions=acquisitions,
        profiles=tuple(args.profiles),
        official_population=len(official_codes),
        eligible_population=len(all_codes),
        eligibility={
            "schema_version": eligibility_status.schema_version,
            "fact_count": eligibility_status.fact_count,
            "excluded_fact_codes": eligibility_status.excluded_count,
            "integrity_ok": eligibility_status.integrity_ok,
            "refresh_state": eligibility_status.refresh_state,
            "last_error": eligibility_status.last_error,
        },
        compared_sources=tuple(args.sources),
        baostock_results=baostock_results,
        preparation=preparation,
        reference_setup_ms=setup_ms,
        industry_probe=industry_probe,
        auxiliary_report=auxiliary.report,
        budget=budget.report(),
    )
    summary["official_reference"] = {"mode": official_mode, "rows": len(official_codes)}
    recommended = summary["recommended_plan"]
    assert isinstance(recommended, dict)
    findings: list[dict[str, object]] = []
    if official_mode != "fresh_exchange":
        findings.append(
            {
                "severity": "warning",
                "code": "official_reference_cached",
                "message": "Exchange request failed; source comparison uses previously validated local issuer identities.",
                "evidence": {"rows": len(official_codes)},
            }
        )
    unresolved = recommended.get("unresolved_source_inputs")
    if isinstance(unresolved, list) and unresolved:
        findings.append(
            {
                "severity": "warning",
                "code": "source_inputs_incomplete",
                "message": "No Tencent-primary route supplied every source-owned freeze input in every measured round.",
                "evidence": {"selected_route": recommended.get("selected_route"), "fields": unresolved},
            }
        )
    source_results = summary.get("source_results")
    failed_sources = (
        [
            {"source": item.get("source"), "errors": item.get("errors")}
            for item in source_results
            if isinstance(item, dict) and (item.get("failed_rounds") or item.get("network_status") == "failed")
        ]
        if isinstance(source_results, list)
        else []
    )
    if failed_sources:
        findings.append(
            {
                "severity": "warning",
                "code": "market_source_failures",
                "message": "At least one standalone supplier round failed.",
                "evidence": failed_sources,
            }
        )
    test_budget = summary.get("test_budget")
    excluded_sources = test_budget.get("excluded_sources") if isinstance(test_budget, dict) else None
    if isinstance(excluded_sources, list) and excluded_sources:
        findings.append(
            {
                "severity": "warning",
                "code": "market_source_test_budget_exhausted",
                "message": "Slow market sources were excluded from the remaining diagnostic routes.",
                "evidence": excluded_sources,
            }
        )
    return {
        "schema_version": "market-factor-comparison",
        "status": "degraded"
        if any(sample["status"] == "measured" for sample in samples)
        or baostock_capability is not None
        and bool(baostock_capability.published_industries)
        else "failed",
        "summary": summary,
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--qfq-database", type=Path, required=True)
    parser.add_argument("--cpu-profile-dir", type=Path)
    parser.add_argument("--sources", nargs="+", choices=_SOURCES, default=list(_SOURCES))
    parser.add_argument("--profiles", nargs="+", choices=("v2", "v3"), default=["v2", "v3"])
    parser.add_argument("--sizes", nargs="+", type=int, default=[120, 720, 0])
    parser.add_argument("--mode", choices=("single", "combined", "all"), default="all")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--interval-seconds", type=float, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=5)
    parser.add_argument("--batch-seconds", type=float, default=30)
    parser.add_argument("--source-loop-seconds", type=float, default=MARKET_SOURCE_LOOP_LIMIT_SECONDS)
    parser.add_argument("--task-seconds", type=float, default=MARKET_TASK_LIMIT_SECONDS)
    args = parser.parse_args()
    if (
        not 1 <= args.rounds <= 100
        or len(args.sizes) > 3
        or any(size not in (0, 120, 720) for size in args.sizes)
        or not all(math.isfinite(value) for value in (args.interval_seconds, args.timeout_seconds, args.batch_seconds))
        or not math.isfinite(args.source_loop_seconds)
        or not math.isfinite(args.task_seconds)
        or not 0 <= args.interval_seconds <= 60
        or not 0 < args.timeout_seconds <= 30
        or not 0 < args.batch_seconds <= 120
        or not 0 < args.source_loop_seconds <= MARKET_SOURCE_LOOP_LIMIT_SECONDS
        or not 0 < args.task_seconds <= MARKET_TASK_LIMIT_SECONDS
        or len(set(args.sources)) != len(args.sources)
        or len(set(args.profiles)) != len(args.profiles)
        or len(set(args.sizes)) != len(args.sizes)
        or args.mode != "single"
        and ("tencent" not in args.sources or len(args.sources) < 2)
    ):
        parser.error("comparison requires bounded distinct sources, sizes, rounds and deadlines")
    sys.stdout.flush()
    result_descriptor = os.dup(1)
    try:
        os.dup2(2, 1)
        try:
            with redirect_stdout(sys.stderr):
                report = Collect(args, cpu_profile_dir=args.cpu_profile_dir)
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
