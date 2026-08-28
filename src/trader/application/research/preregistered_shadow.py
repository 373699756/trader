"""Collect and evaluate the fixed Tomorrow preregistered shadow family."""

from __future__ import annotations

import dataclasses
import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.application.research.ports import PreregisteredShadowEvidencePort
from trader.application.research.preregistered_shadow_models import (
    PreregisteredShadowCostSensitivity,
    PreregisteredShadowDayRecord,
    PreregisteredShadowGateReport,
    PreregisteredShadowPair,
    PreregisteredShadowVariantGate,
    ShadowGateScope,
    ShadowGateState,
    ShadowProductionScope,
    preregistered_shadow_evidence_manifest_hash,
)
from trader.domain.research.baseline import mean_rank_ic, population_spearman, quantile_bucket, stock_net_contribution
from trader.domain.research.paired_statistics import (
    PreregisteredBootstrapPlan,
    PreregisteredBootstrapResult,
    PreregisteredHolmDecision,
    fixed_family_holm,
    paired_moving_block_statistics,
)
from trader.domain.research.tomorrow_shadow_preregistration import (
    TOMORROW_SHADOW_CHALLENGER_FAMILY,
    TOMORROW_SHADOW_P1_SPEC,
    TomorrowShadowCalendarAttestation,
    TomorrowShadowChallengerId,
    TomorrowShadowPreregistration,
)


class PreregisteredShadowCollector:
    """Append evidence only after calendar and historical eligibility checks."""

    def __init__(
        self,
        attestation: TomorrowShadowCalendarAttestation,
        store: PreregisteredShadowEvidencePort,
        historical_report: PreregisteredShadowGateReport | None = None,
    ) -> None:
        self._attestation = attestation
        self._store = store
        self._historical_report = historical_report
        if historical_report is not None:
            if historical_report.calendar_attestation_hash != attestation.content_hash:
                raise ValueError("historical shadow gate calendar attestation does not match")
            if historical_report.scope != "historical" or historical_report.state != "historical_passed":
                raise ValueError("forward shadow evidence requires a completed historical gate report")

    def append(self, record: PreregisteredShadowDayRecord) -> PreregisteredShadowDayRecord:
        if record.calendar_attestation_hash != self._attestation.content_hash:
            raise ValueError("shadow evidence calendar attestation does not match")
        if record.phase == "forward":
            if self._historical_report is None or self._historical_report.scope != "historical":
                raise ValueError("forward shadow evidence requires a historical gate report")
            gate = next(item for item in self._historical_report.variants if item.challenger_id == record.challenger_id)
            if gate.state != "passed":
                raise ValueError("forward shadow evidence requires a historically passed challenger")
            if record.historical_gate_hash != self._historical_report.content_hash:
                raise ValueError("forward shadow evidence historical gate does not match")
        return self._store.append(record)


class PreregisteredShadowGate:
    """Recompute the fixed family without allowing dates, variants, or thresholds to drift."""

    def __init__(
        self,
        attestation: TomorrowShadowCalendarAttestation,
        spec: TomorrowShadowPreregistration = TOMORROW_SHADOW_P1_SPEC,
    ) -> None:
        if attestation.research_spec_hash != spec.content_hash:
            raise ValueError("shadow gate calendar attestation does not match its spec")
        self._attestation = attestation
        self._spec = spec

    def evaluate(
        self,
        records: tuple[PreregisteredShadowDayRecord, ...],
        *,
        scope: ShadowGateScope,
        historical_report: PreregisteredShadowGateReport | None = None,
    ) -> PreregisteredShadowGateReport:
        if scope not in {"historical", "forward", "combined"}:
            raise ValueError("shadow gate scope is invalid")
        if scope == "historical":
            if historical_report is not None:
                raise ValueError("historical shadow gate cannot bind a previous historical report")
        elif (
            historical_report is None
            or historical_report.scope != "historical"
            or historical_report.state != "historical_passed"
            or historical_report.research_spec_hash != self._spec.content_hash
            or historical_report.calendar_attestation_hash != self._attestation.content_hash
        ):
            raise ValueError("forward shadow gate requires its matching historical gate report")
        expected_dates = _scope_dates(self._spec, scope)
        self._validate_records(
            records,
            expected_dates,
            scope,
            historical_report,
        )
        by_variant = {
            challenger: tuple(
                sorted(
                    (record for record in records if record.challenger_id == challenger),
                    key=lambda item: item.planned_trade_date,
                )
            )
            for challenger in TOMORROW_SHADOW_CHALLENGER_FAMILY
        }
        prepared: list[PreregisteredShadowVariantGate] = []
        for challenger in TOMORROW_SHADOW_CHALLENGER_FAMILY:
            if (
                historical_report is not None
                and next(item for item in historical_report.variants if item.challenger_id == challenger).state
                != "passed"
            ):
                prepared.append(_empty_variant(challenger, "rejected", "historical_gate_failed", 0))
            else:
                prepared.append(_prepare_variant(challenger, by_variant[challenger], expected_dates, scope, self._spec))
        p_values: dict[str, float | None] = {
            item.challenger_id: _primary_bootstrap(item, self._spec.primary_block_days).p_value
            if item.bootstrap
            else None
            for item in prepared
        }
        holm = fixed_family_holm(
            p_values,
            family=TOMORROW_SHADOW_CHALLENGER_FAMILY,
            alpha=self._spec.holm_alpha,
        )
        holm_by_id = {item.challenger_id: item for item in holm}
        variants = tuple(_apply_holm(item, holm_by_id[item.challenger_id], self._spec) for item in prepared)
        state = _report_state(scope, variants)
        return PreregisteredShadowGateReport(
            self._spec.content_hash,
            self._attestation.content_hash,
            preregistered_shadow_evidence_manifest_hash(records),
            historical_report.content_hash if historical_report is not None else None,
            scope,
            state,
            variants,
            holm,
        )

    def _validate_records(
        self,
        records: tuple[PreregisteredShadowDayRecord, ...],
        expected_dates: tuple[date, ...],
        scope: ShadowGateScope,
        historical_report: PreregisteredShadowGateReport | None,
    ) -> None:
        keys = tuple((item.challenger_id, item.phase, item.planned_trade_date) for item in records)
        if len(keys) != len(set(keys)):
            raise ValueError("preregistered shadow gate received duplicate evidence identities")
        if any(item.research_spec_hash != self._spec.content_hash for item in records):
            raise ValueError("preregistered shadow gate spec identity does not match")
        if any(item.calendar_attestation_hash != self._attestation.content_hash for item in records):
            raise ValueError("preregistered shadow gate calendar attestation is inconsistent")
        if historical_report is not None and any(
            item.phase == "forward" and item.historical_gate_hash != historical_report.content_hash
            for item in records
        ):
            raise ValueError("preregistered shadow gate historical binding is inconsistent")
        expected = set(expected_dates)
        if any(item.planned_trade_date not in expected for item in records):
            raise ValueError("preregistered shadow gate received a date outside its scope")
        if scope != "combined" and any(item.phase != scope for item in records):
            raise ValueError("preregistered shadow gate evidence phase does not match")


def _scope_dates(spec: TomorrowShadowPreregistration, scope: ShadowGateScope) -> tuple[date, ...]:
    if scope == "historical":
        return spec.historical_dates
    if scope == "forward":
        return spec.forward_dates
    return (*spec.historical_dates, *spec.forward_dates)


def _prepare_variant(
    challenger_id: TomorrowShadowChallengerId,
    records: tuple[PreregisteredShadowDayRecord, ...],
    expected_dates: tuple[date, ...],
    scope: ShadowGateScope,
    spec: TomorrowShadowPreregistration,
) -> PreregisteredShadowVariantGate:
    observed_dates = {item.planned_trade_date for item in records}
    if observed_dates != set(expected_dates):
        return _empty_variant(challenger_id, "collecting", "planned_dates_incomplete", len(records))
    if any(item.status == "failed" for item in records):
        return _empty_variant(challenger_id, "rejected", "failed_planned_day", len(records))
    metrics = _metrics(records, challenger_id, spec)
    failures = _metric_failures(metrics, scope, spec)
    return PreregisteredShadowVariantGate(
        challenger_id=challenger_id,
        state="rejected" if failures else "passed",
        day_count=len(records),
        pair_count=metrics.pair_count,
        cost_sensitivities=metrics.cost_sensitivities,
        baseline_severe_rate=metrics.baseline_severe_rate,
        challenger_severe_rate=metrics.challenger_severe_rate,
        turnover_increase=metrics.turnover_increase,
        oracle_recall=metrics.oracle_recall,
        delete_best_month_increment=metrics.delete_best_month_increment,
        delete_best_board_increment=metrics.delete_best_board_increment,
        maximum_stock_positive_fraction=metrics.maximum_stock_positive_fraction,
        top_five_positive_fraction=metrics.top_five_positive_fraction,
        top_quintile_net_excess=metrics.top_quintile_net_excess,
        bottom_quintile_net_excess=metrics.bottom_quintile_net_excess,
        mean_rank_ic=metrics.mean_rank_ic,
        hybrid_primary_bootstrap=metrics.hybrid_primary_bootstrap,
        production_scope="none" if failures else _production_scope(metrics),
        failure_reasons=failures,
    )


def _empty_variant(
    challenger_id: TomorrowShadowChallengerId,
    state: Literal["collecting", "rejected"],
    reason: str,
    day_count: int,
) -> PreregisteredShadowVariantGate:
    return PreregisteredShadowVariantGate(
        challenger_id=challenger_id,
        state=state,
        day_count=day_count,
        pair_count=0,
        cost_sensitivities=(),
        baseline_severe_rate=None,
        challenger_severe_rate=None,
        turnover_increase=None,
        oracle_recall=None,
        delete_best_month_increment=None,
        delete_best_board_increment=None,
        maximum_stock_positive_fraction=None,
        top_five_positive_fraction=None,
        top_quintile_net_excess=None,
        bottom_quintile_net_excess=None,
        mean_rank_ic=None,
        hybrid_primary_bootstrap=None,
        production_scope="none",
        failure_reasons=(reason,),
    )


@dataclass(frozen=True)
class _VariantMetrics:
    pair_count: int
    cost_sensitivities: tuple[PreregisteredShadowCostSensitivity, ...]
    baseline_severe_rate: float
    challenger_severe_rate: float
    turnover_increase: float
    oracle_recall: float | None
    delete_best_month_increment: float
    delete_best_board_increment: float
    maximum_stock_positive_fraction: float | None
    top_five_positive_fraction: float | None
    top_quintile_net_excess: float | None
    bottom_quintile_net_excess: float | None
    mean_rank_ic: float | None
    hybrid_primary_bootstrap: PreregisteredBootstrapResult


def _metrics(
    records: tuple[PreregisteredShadowDayRecord, ...],
    challenger_id: TomorrowShadowChallengerId,
    spec: TomorrowShadowPreregistration,
) -> _VariantMetrics:
    cost_sensitivities = tuple(
        _cost_sensitivity(records, challenger_id, spec, cost_rate) for cost_rate in spec.cost_rates
    )
    daily = tuple(_daily_metrics(record, spec.cost_rates[0]) for record in records)
    increments = tuple(item[0] for item in daily)
    hybrid_increments = tuple(item[1] for item in daily)
    hybrid = paired_moving_block_statistics(
        hybrid_increments,
        plan=PreregisteredBootstrapPlan(
            identity=f"{spec.research_identity}|hybrid",
            master_seed=spec.bootstrap_master_seed,
            challenger_id=challenger_id,
            block_days=spec.primary_block_days,
            repetitions=spec.bootstrap_repetitions,
        ),
    )
    pairs = tuple(pair for record in records for pair in record.pairs)
    baseline_severe = _weighted_rate(pairs, "baseline", lambda pair: pair.mae_atr20 <= -1.5)
    challenger_severe = _weighted_rate(pairs, "challenger", lambda pair: pair.mae_atr20 <= -1.5)
    baseline_turnover = _weighted_average(pairs, "baseline", lambda pair: pair.turnover)
    challenger_turnover = _weighted_average(pairs, "challenger", lambda pair: pair.turnover)
    oracle_rows = tuple(pair for pair in pairs if pair.oracle_member)
    oracle_recall = (
        sum(pair.challenger_weight > 0.0 for pair in oracle_rows) / len(oracle_rows) if oracle_rows else None
    )
    month_groups: dict[str, float] = defaultdict(float)
    for record, increment in zip(records, increments, strict=True):
        month_groups[record.planned_trade_date.strftime("%Y-%m")] += increment
    stock_contributions, board_contributions = _positive_contribution_inputs(records, spec.cost_rates[0])
    maximum_stock, top_five = _concentration(stock_contributions)
    top, bottom, rank_ic = _score_diagnostics(records, spec.cost_rates[0])
    return _VariantMetrics(
        pair_count=len(pairs),
        cost_sensitivities=cost_sensitivities,
        baseline_severe_rate=baseline_severe,
        challenger_severe_rate=challenger_severe,
        turnover_increase=challenger_turnover - baseline_turnover,
        oracle_recall=oracle_recall,
        delete_best_month_increment=_delete_best_group(month_groups),
        delete_best_board_increment=_delete_best_group(board_contributions),
        maximum_stock_positive_fraction=maximum_stock,
        top_five_positive_fraction=top_five,
        top_quintile_net_excess=top,
        bottom_quintile_net_excess=bottom,
        mean_rank_ic=rank_ic,
        hybrid_primary_bootstrap=hybrid,
    )


def _cost_sensitivity(
    records: tuple[PreregisteredShadowDayRecord, ...],
    challenger_id: TomorrowShadowChallengerId,
    spec: TomorrowShadowPreregistration,
    cost_rate: float,
) -> PreregisteredShadowCostSensitivity:
    increments = tuple(_daily_metrics(record, cost_rate)[0] for record in records)
    bootstrap = tuple(
        paired_moving_block_statistics(
            increments,
            plan=PreregisteredBootstrapPlan(
                identity=spec.research_identity,
                master_seed=spec.bootstrap_master_seed,
                challenger_id=challenger_id,
                block_days=block_days,
                repetitions=spec.bootstrap_repetitions,
            ),
        )
        for block_days in spec.bootstrap_block_days
    )
    return PreregisteredShadowCostSensitivity(
        cost_rate=cost_rate,
        mean_increment=math.fsum(increments) / len(increments),
        bootstrap=bootstrap,
    )


def _daily_metrics(record: PreregisteredShadowDayRecord, cost_rate: float) -> tuple[float, float]:
    baseline = math.fsum(_contribution(pair, pair.baseline_weight, cost_rate) for pair in record.pairs)
    challenger = math.fsum(_contribution(pair, pair.challenger_weight, cost_rate) for pair in record.pairs)
    hybrid = math.fsum(_contribution(pair, pair.hybrid_weight, cost_rate) for pair in record.pairs)
    return challenger - baseline, hybrid - challenger


def _contribution(pair: PreregisteredShadowPair, weight: float, cost_rate: float) -> float:
    return stock_net_contribution(weight, pair.gross_excess_return, pair.turnover, cost_rate)


def _weighted_rate(
    pairs: tuple[PreregisteredShadowPair, ...],
    track: str,
    predicate: Callable[[PreregisteredShadowPair], bool],
) -> float:
    weights = tuple(_weight(pair, track) for pair in pairs)
    denominator = math.fsum(weights)
    if denominator == 0.0:
        return 0.0
    return math.fsum(weight for pair, weight in zip(pairs, weights, strict=True) if predicate(pair)) / denominator


def _weighted_average(
    pairs: tuple[PreregisteredShadowPair, ...],
    track: str,
    value: Callable[[PreregisteredShadowPair], float],
) -> float:
    weights = tuple(_weight(pair, track) for pair in pairs)
    denominator = math.fsum(weights)
    if denominator == 0.0:
        return 0.0
    return math.fsum(weight * value(pair) for pair, weight in zip(pairs, weights, strict=True)) / denominator


def _weight(pair: PreregisteredShadowPair, track: str) -> float:
    return pair.baseline_weight if track == "baseline" else pair.challenger_weight


def _positive_contribution_inputs(
    records: tuple[PreregisteredShadowDayRecord, ...],
    cost_rate: float,
) -> tuple[dict[str, float], dict[str, float]]:
    stocks: dict[str, float] = defaultdict(float)
    boards: dict[str, float] = defaultdict(float)
    for record in records:
        for pair in record.pairs:
            difference = _contribution(pair, pair.challenger_weight, cost_rate) - _contribution(
                pair, pair.baseline_weight, cost_rate
            )
            stocks[pair.code] += difference
            boards[pair.board] += difference
    return stocks, boards


def _concentration(contributions: dict[str, float]) -> tuple[float | None, float | None]:
    positive = sorted((value for value in contributions.values() if value > 0.0), reverse=True)
    denominator = math.fsum(positive)
    if denominator == 0.0:
        return None, None
    return positive[0] / denominator, math.fsum(positive[:5]) / denominator


def _delete_best_group(groups: dict[str, float]) -> float:
    positive = tuple(value for value in groups.values() if value > 0.0)
    remaining = math.fsum(groups.values()) - (max(positive) if positive else 0.0)
    return remaining


def _score_diagnostics(
    records: tuple[PreregisteredShadowDayRecord, ...],
    cost_rate: float,
) -> tuple[float | None, float | None, float | None]:
    top_values: list[float] = []
    bottom_values: list[float] = []
    rank_values: list[float | None] = []
    for record in records:
        buckets = quantile_bucket(tuple((pair.code, pair.score) for pair in record.pairs))
        values = {pair.code: pair.gross_excess_return - pair.turnover * cost_rate for pair in record.pairs}
        top_values.extend(values[code] for code, bucket in buckets.items() if bucket == 5)
        bottom_values.extend(values[code] for code, bucket in buckets.items() if bucket == 1)
        rank_values.append(population_spearman(tuple((pair.score, values[pair.code]) for pair in record.pairs)))
    top = math.fsum(top_values) / len(top_values) if top_values else None
    bottom = math.fsum(bottom_values) / len(bottom_values) if bottom_values else None
    return top, bottom, mean_rank_ic(tuple(rank_values))


def _metric_failures(
    metrics: _VariantMetrics,
    scope: ShadowGateScope,
    spec: TomorrowShadowPreregistration,
) -> tuple[str, ...]:
    minimum_pairs = spec.minimum_forward_pairs if scope == "forward" else spec.minimum_total_pairs
    primary = _primary_bootstrap_from_metrics(metrics, spec.primary_block_days)
    failures: list[str] = []
    checks = (
        (metrics.pair_count < minimum_pairs, "paired_sample_floor"),
        (
            metrics.cost_sensitivities[0].mean_increment < spec.minimum_mean_increment,
            "mean_increment_floor",
        ),
        (not primary.valid or primary.confidence_lower is None or primary.confidence_lower <= 0.0, "bootstrap_ci"),
        (metrics.challenger_severe_rate > metrics.baseline_severe_rate, "severe_loss_rate"),
        (metrics.turnover_increase > spec.maximum_turnover_increase, "turnover_limit"),
        (metrics.oracle_recall is None or metrics.oracle_recall < spec.minimum_oracle_recall, "oracle_recall"),
        (metrics.delete_best_month_increment <= 0.0, "delete_best_month"),
        (metrics.delete_best_board_increment < 0.0, "delete_best_board"),
        (
            metrics.maximum_stock_positive_fraction is None
            or metrics.maximum_stock_positive_fraction > spec.maximum_stock_positive_fraction,
            "stock_concentration",
        ),
        (
            metrics.top_five_positive_fraction is None
            or metrics.top_five_positive_fraction > spec.maximum_top_five_positive_fraction,
            "top_five_concentration",
        ),
        (
            metrics.top_quintile_net_excess is None
            or metrics.bottom_quintile_net_excess is None
            or metrics.top_quintile_net_excess <= metrics.bottom_quintile_net_excess,
            "quintile_monotonicity",
        ),
        (metrics.mean_rank_ic is None or metrics.mean_rank_ic <= 0.0, "rank_ic"),
    )
    failures.extend(reason for failed, reason in checks if failed)
    return tuple(sorted(failures))


def _primary_bootstrap_from_metrics(metrics: _VariantMetrics, block_days: int) -> PreregisteredBootstrapResult:
    return next(item for item in metrics.cost_sensitivities[0].bootstrap if item.block_days == block_days)


def _primary_bootstrap(gate: PreregisteredShadowVariantGate, block_days: int) -> PreregisteredBootstrapResult:
    return next(item for item in gate.bootstrap if item.block_days == block_days)


def _production_scope(metrics: _VariantMetrics) -> ShadowProductionScope:
    hybrid = metrics.hybrid_primary_bootstrap
    if hybrid.valid and hybrid.confidence_lower is not None and hybrid.confidence_lower > 0.0:
        return "hybrid"
    return "local_only"


def _apply_holm(
    gate: PreregisteredShadowVariantGate,
    holm: PreregisteredHolmDecision,
    spec: TomorrowShadowPreregistration,
) -> PreregisteredShadowVariantGate:
    if gate.state != "passed":
        return gate
    if not holm.rejected_null:
        return dataclasses.replace(
            gate, state="rejected", production_scope="none", failure_reasons=("holm_not_rejected",)
        )
    primary = _primary_bootstrap(gate, spec.primary_block_days)
    if primary.confidence_lower is None or primary.confidence_lower <= 0.0:
        return dataclasses.replace(gate, state="rejected", production_scope="none", failure_reasons=("bootstrap_ci",))
    return gate


def _report_state(
    scope: ShadowGateScope,
    variants: tuple[PreregisteredShadowVariantGate, ...],
) -> ShadowGateState:
    if any(item.state == "collecting" for item in variants):
        return "collecting"
    if not any(item.state == "passed" for item in variants):
        return "rejected"
    if scope == "historical":
        return "historical_passed"
    if scope == "forward":
        return "forward_passed"
    return "promotion_eligible"


__all__ = ["PreregisteredShadowCollector", "PreregisteredShadowGate"]
