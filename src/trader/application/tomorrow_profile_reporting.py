"""Candidate- and portfolio-layer reporting for paired Tomorrow V1/V2 evidence."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import cast

from trader.application.ports.tomorrow_profile_comparison import TomorrowProfileEvidencePort
from trader.domain.outcome.models import RecommendationOutcome
from trader.domain.research.baseline import mean_rank_ic, population_spearman, quantile_bucket
from trader.domain.research.paired_statistics import (
    PreregisteredBootstrapPlan,
    PreregisteredBootstrapResult,
    paired_moving_block_statistics,
)
from trader.domain.research.tomorrow_profile_comparison import (
    TomorrowProfileComparisonReport,
    TomorrowProfileComparisonSpec,
    TomorrowProfileId,
    TomorrowProfileLayerMetrics,
    TomorrowProfilePair,
)


@dataclass(frozen=True)
class _ObservedPair:
    pair: TomorrowProfilePair
    outcome: RecommendationOutcome


@dataclass(frozen=True)
class _ProfileSeries:
    metrics: TomorrowProfileLayerMetrics
    daily_20: tuple[float, ...]
    daily_50: tuple[float, ...]
    daily_100: tuple[float, ...]


@dataclass(frozen=True)
class _EvidenceCounts:
    paired_candidates: int
    independent_days: int


class TomorrowProfileReportingService:
    def __init__(self, spec: TomorrowProfileComparisonSpec, evidence: TomorrowProfileEvidencePort) -> None:
        self._spec = spec
        self._evidence = evidence

    def report(self) -> TomorrowProfileComparisonReport:
        outcomes = {(item.snapshot_id, item.stock_code): item for item in self._evidence.settled_outcomes()}
        grouped: dict[str, list[_ObservedPair]] = defaultdict(list)
        for manifest in self._evidence.formal_manifests():
            manifest_outcomes = tuple(outcomes.get((manifest.input_version, pair.code)) for pair in manifest.pairs)
            complete_count = sum(item is not None and item.status == "complete" for item in manifest_outcomes)
            if any(item is None for item in manifest_outcomes):
                continue
            if complete_count < self._spec.minimum_paired_candidates:
                continue
            for pair in manifest.pairs:
                outcome = outcomes.get((manifest.input_version, pair.code))
                if outcome is not None and outcome.status == "complete":
                    grouped[manifest.trade_date.isoformat()].append(_ObservedPair(pair, outcome))
        observed = tuple((day, tuple(grouped[day])) for day in sorted(grouped))
        v1 = _profile_series("v1", observed)
        v2 = _profile_series("v2", observed)
        difference20 = tuple(right - left for left, right in zip(v1.daily_20, v2.daily_20, strict=True))
        difference50 = tuple(right - left for left, right in zip(v1.daily_50, v2.daily_50, strict=True))
        difference100 = tuple(right - left for left, right in zip(v1.daily_100, v2.daily_100, strict=True))
        bootstrap = paired_moving_block_statistics(
            difference20,
            plan=PreregisteredBootstrapPlan(
                identity=self._spec.research_identity,
                master_seed=self._spec.bootstrap_master_seed,
                challenger_id="v2_minus_v1_portfolio_20bp",
                block_days=self._spec.bootstrap_block_days,
                repetitions=self._spec.bootstrap_repetitions,
            ),
        )
        pair_count = sum(len(items) for _day, items in observed)
        counts = _EvidenceCounts(pair_count, len(observed))
        failures = _gate_failures(self._spec, counts, v1, v2, bootstrap)
        enough = (
            len(observed) >= self._spec.required_independent_days and pair_count >= self._spec.minimum_paired_candidates
        )
        state = "review_ready" if enough and not failures else "rejected" if enough else "collecting"
        return TomorrowProfileComparisonReport(
            spec_hash=self._spec.content_hash,
            independent_days=len(observed),
            paired_candidates=pair_count,
            v1=v1.metrics,
            v2=v2.metrics,
            daily_v2_minus_v1_20bp_pct=difference20,
            daily_v2_minus_v1_50bp_pct=difference50,
            daily_v2_minus_v1_100bp_pct=difference100,
            primary_bootstrap=bootstrap,
            gate_failures=failures,
            state=state,  # type: ignore[arg-type]
            manual_review_eligible=state == "review_ready",
        )


def _profile_series(
    profile_id: TomorrowProfileId,
    observed: tuple[tuple[str, tuple[_ObservedPair, ...]], ...],
) -> _ProfileSeries:
    rank_ics: list[float | None] = []
    quintile_spreads: list[float] = []
    candidate_returns: list[float] = []
    severe: list[float] = []
    daily: list[tuple[float, float, float]] = []
    turnovers: list[float] = []
    positive: dict[str, float] = defaultdict(float)
    previous: frozenset[str] = frozenset()
    for _day, items in observed:
        predictions = tuple((item.pair.v1 if profile_id == "v1" else item.pair.v2, item) for item in items)
        complete = tuple(
            (prediction, item)
            for prediction, item in predictions
            if item.outcome.net_excess_return_pct is not None
            and item.outcome.gross_return_pct is not None
            and item.outcome.benchmark_return_pct is not None
        )
        score_pairs = tuple(
            (prediction.signal_score, cast(float, item.outcome.net_excess_return_pct)) for prediction, item in complete
        )
        rank_ics.append(population_spearman(score_pairs))
        buckets = quantile_bucket(tuple((item.pair.code, prediction.signal_score) for prediction, item in complete))
        top = tuple(
            cast(float, item.outcome.net_excess_return_pct)
            for prediction, item in complete
            if buckets[item.pair.code] == 5
        )
        bottom = tuple(
            cast(float, item.outcome.net_excess_return_pct)
            for prediction, item in complete
            if buckets[item.pair.code] == 1
        )
        if top and bottom:
            quintile_spreads.append(_mean(top) - _mean(bottom))
        candidate_returns.extend(cast(float, item.outcome.net_excess_return_pct) for _prediction, item in complete)
        selected = frozenset(item.pair.code for prediction, item in complete if prediction.selected)
        selected_items = tuple(item for prediction, item in complete if prediction.selected)
        turnover = len(selected - previous) / len(selected) if selected else 0.0
        turnovers.append(turnover)
        computed_day_values = tuple(
            _portfolio_return(selected_items, selected - previous, cost) for cost in (0.20, 0.50, 1.00)
        )
        day_values = (computed_day_values[0], computed_day_values[1], computed_day_values[2])
        daily.append(day_values)
        for item in selected_items:
            if item.outcome.severe_drawdown is not None:
                severe.append(1.0 if item.outcome.severe_drawdown else 0.0)
            value = max(0.0, _stock_excess_before_cost(item) - (0.20 if item.pair.code in selected - previous else 0.0))
            positive[item.pair.code] += value / len(selected_items) if selected_items else 0.0
        previous = selected
    shares = sorted(positive.values(), reverse=True)
    total_positive = math.fsum(shares)
    fractions = tuple(value / total_positive for value in shares) if total_positive else ()
    metrics = TomorrowProfileLayerMetrics(
        profile_id=profile_id,
        candidate_pairs=sum(len(items) for _day, items in observed),
        portfolio_days=len(observed),
        mean_candidate_net_excess_pct=_optional_mean(tuple(candidate_returns)),
        mean_rank_ic=mean_rank_ic(tuple(rank_ics)),
        top_bottom_quintile_spread_pct=_optional_mean(tuple(quintile_spreads)),
        mean_portfolio_net_excess_20bp_pct=_optional_mean(tuple(item[0] for item in daily)),
        mean_portfolio_net_excess_50bp_pct=_optional_mean(tuple(item[1] for item in daily)),
        mean_portfolio_net_excess_100bp_pct=_optional_mean(tuple(item[2] for item in daily)),
        severe_loss_rate=_optional_mean(tuple(severe)),
        mean_turnover=_optional_mean(tuple(turnovers)),
        maximum_stock_positive_fraction=fractions[0] if fractions else None,
        top_five_positive_fraction=math.fsum(fractions[:5]) if fractions else None,
    )
    return _ProfileSeries(
        metrics,
        tuple(item[0] for item in daily),
        tuple(item[1] for item in daily),
        tuple(item[2] for item in daily),
    )


def _portfolio_return(items: tuple[_ObservedPair, ...], new_codes: frozenset[str], cost_pct: float) -> float:
    if not items:
        return 0.0
    return _mean(
        tuple(_stock_excess_before_cost(item) - (cost_pct if item.pair.code in new_codes else 0.0) for item in items)
    )


def _stock_excess_before_cost(item: _ObservedPair) -> float:
    outcome = item.outcome
    if outcome.gross_return_pct is None or outcome.benchmark_return_pct is None:
        raise ValueError("complete Tomorrow pair is missing return evidence")
    return outcome.gross_return_pct - outcome.benchmark_return_pct


def _gate_failures(
    spec: TomorrowProfileComparisonSpec,
    counts: _EvidenceCounts,
    v1: _ProfileSeries,
    v2: _ProfileSeries,
    bootstrap: PreregisteredBootstrapResult,
) -> tuple[str, ...]:
    failures: list[str] = []
    if counts.independent_days < spec.required_independent_days:
        failures.append("independent_days_below_power")
    if counts.paired_candidates < spec.minimum_paired_candidates:
        failures.append("paired_candidates_below_minimum")
    if not bootstrap.valid or bootstrap.confidence_lower is None or bootstrap.confidence_lower <= 0.0:
        failures.append("v2_net_excess_lower_bound_not_positive")
    if (
        v1.metrics.severe_loss_rate is None
        or v2.metrics.severe_loss_rate is None
        or v2.metrics.severe_loss_rate > v1.metrics.severe_loss_rate
    ):
        failures.append("v2_severe_loss_rate_worse")
    if (
        v1.metrics.mean_turnover is None
        or v2.metrics.mean_turnover is None
        or v2.metrics.mean_turnover - v1.metrics.mean_turnover > spec.maximum_turnover_increase
    ):
        failures.append("v2_turnover_increase")
    if v2.metrics.mean_rank_ic is None or v2.metrics.mean_rank_ic <= 0.0:
        failures.append("v2_rank_ic_not_positive")
    if v2.metrics.top_bottom_quintile_spread_pct is None or v2.metrics.top_bottom_quintile_spread_pct <= 0.0:
        failures.append("v2_quintile_spread_not_positive")
    for name, value, limit in (
        ("stock", v2.metrics.maximum_stock_positive_fraction, spec.maximum_stock_positive_fraction),
        ("top_five", v2.metrics.top_five_positive_fraction, spec.maximum_top_five_positive_fraction),
    ):
        if value is None or value > limit:
            failures.append(f"v2_{name}_concentration")
    return tuple(failures)


def _mean(values: tuple[float, ...]) -> float:
    return math.fsum(values) / len(values)


def _optional_mean(values: tuple[float, ...]) -> float | None:
    return _mean(values) if values else None


__all__ = ["TomorrowProfileReportingService"]
