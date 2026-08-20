from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from typing import Literal

from trader.application.research.challenger_models import (
    ChallengerDayReplay,
    ChallengerSameStockPair,
    ChallengerVariantReplay,
    ScoreR4ChallengerReport,
)
from trader.application.research.ports import ScoreR5ForwardEvidencePort
from trader.application.research.replay_models import ScoreR3BaselineReport
from trader.application.research.score_r5_models import (
    ScoreR5FinalReport,
    ScoreR5ForwardBindings,
    ScoreR5ForwardDayRecord,
    ScoreR5HistoricalReport,
    ScoreR5HybridIncrement,
    ScoreR5TrackMetrics,
    ScoreR5VariantGate,
)
from trader.domain.research.baseline import mean_rank_ic, population_spearman, quantile_bucket
from trader.domain.research.challengers import ChallengerVariantId
from trader.domain.research.specification import SCORE_P0_V1_SPEC, ScoreResearchSpec, get_score_research_spec
from trader.domain.research.statistics import (
    BOOTSTRAP_BLOCK_DAYS,
    PRIMARY_BLOCK_DAYS,
    HolmDecision,
    holm_step_down,
    paired_moving_block_bootstrap,
)

_COST_RATES = (0.002, 0.005, 0.01)


@dataclass(frozen=True)
class _DailyTrack:
    trade_date: date
    pair_count: int
    cost_differences: tuple[float, float, float]
    baseline_severe_rate: float
    challenger_severe_rate: float
    recalled_oracle_count: int
    oracle_count: int
    stock_contributions_20bp: tuple[tuple[str, float], ...]
    board_contributions_20bp: tuple[tuple[str, float], ...]
    top_quintile_net_excess_20bp: float | None
    bottom_quintile_net_excess_20bp: float | None
    rank_ic: float | None
    high_minus_low_severe_rate: float | None


class ScoreR5StatisticalGate:
    """Evaluate the fixed five challengers without changing production state."""

    def __init__(self, spec: ScoreResearchSpec = SCORE_P0_V1_SPEC) -> None:
        self._spec = spec

    def evaluate(
        self,
        baseline: ScoreR3BaselineReport,
        challengers: ScoreR4ChallengerReport,
    ) -> ScoreR5HistoricalReport:
        _validate_parents(baseline, challengers, self._spec)
        oracle_by_date = {item.trade_date: item.oracle_codes for item in baseline.days}
        prepared = tuple(self._prepare_variant(variant, oracle_by_date) for variant in challengers.variants)
        primary_p = {variant.variant_id: variant.local_track.primary_bootstrap.p_value for variant in prepared}
        holm_by_id = {item.variant_id: item for item in holm_step_down(primary_p)}
        variants = tuple(self._apply_gates(item, holm_by_id[item.variant_id], len(baseline.days)) for item in prepared)
        return ScoreR5HistoricalReport(
            "evaluated" if len(baseline.days) == 40 else "exploratory",
            baseline.report_hash,
            challengers.content_hash,
            challengers.parameter_manifest_hash,
            len(baseline.days),
            variants,
            forward_dates=self._spec.forward_dates,
            research_identity=self._spec.research_identity,
            research_spec_hash=self._spec.content_hash,
            schema_version=(
                "score_r5_statistical_gate_v2"
                if self._spec.research_identity == "score_p0_v2"
                else "score_r5_statistical_gate_v1"
            ),
            statistics_version=(
                "score_r5_paired_mbb_holm_v2"
                if self._spec.research_identity == "score_p0_v2"
                else "score_r5_paired_mbb_holm_v1"
            ),
            report_version=(
                "score_r5_final_report_v2"
                if self._spec.research_identity == "score_p0_v2"
                else "score_r5_final_report_v1"
            ),
        )

    def _prepare_variant(
        self,
        variant: ChallengerVariantReplay,
        oracle_by_date: dict[date, tuple[str, ...]],
    ) -> ScoreR5VariantGate:
        return self._prepare_days(variant.variant_id, variant.variant_version, variant.days, oracle_by_date)

    def _prepare_days(
        self,
        variant_id: ChallengerVariantId,
        variant_version: str,
        days: tuple[ChallengerDayReplay, ...],
        oracle_by_date: dict[date, tuple[str, ...]],
    ) -> ScoreR5VariantGate:
        local = _track_metrics(variant_id, days, oracle_by_date, "local_only", self._spec)
        hybrid = _track_metrics(variant_id, days, oracle_by_date, "hybrid", self._spec)
        increment = _hybrid_increment(variant_id, days, self._spec)
        placeholder = HolmDecision(variant_id, None, 1, 0.01, False)
        return ScoreR5VariantGate(
            variant_id,
            variant_version,
            "historical_rejected",
            ("pending_holm",),
            placeholder,
            local,
            hybrid,
            increment,
        )

    def _apply_gates(
        self,
        prepared: ScoreR5VariantGate,
        holm: HolmDecision,
        historical_day_count: int,
    ) -> ScoreR5VariantGate:
        failures = _gate_failures(prepared.local_track, holm, historical_day_count, 40)
        return ScoreR5VariantGate(
            prepared.variant_id,
            prepared.variant_version,
            "historical_rejected" if failures else "historical_passed",
            failures,
            holm,
            prepared.local_track,
            prepared.hybrid_track,
            prepared.hybrid_increment,
        )


class ScoreR5ForwardCollector:
    """Append immutable evidence only for variants that passed the frozen historical gate."""

    def __init__(
        self,
        historical: ScoreR5HistoricalReport,
        store: ScoreR5ForwardEvidencePort | None = None,
    ) -> None:
        self._historical = historical
        self._store = store
        self._records: dict[tuple[ChallengerVariantId, date], ScoreR5ForwardDayRecord] = {}

    def record_failed(
        self,
        variant_id: ChallengerVariantId,
        planned_trade_date: date,
        reason: str,
    ) -> ScoreR5ForwardDayRecord:
        record = ScoreR5ForwardDayRecord(
            self._bindings(variant_id),
            planned_trade_date,
            "failed",
            None,
            (),
            reason,
            _forward_record_schema(self._historical.research_identity),
        )
        return self._append(record)

    def record_day(
        self,
        variant_id: ChallengerVariantId,
        day: ChallengerDayReplay,
        oracle_codes: tuple[str, ...],
    ) -> ScoreR5ForwardDayRecord:
        status: Literal["valid", "no_decision"] = "no_decision" if day.local_status == "no_decision" else "valid"
        record = ScoreR5ForwardDayRecord(
            self._bindings(variant_id),
            day.trade_date,
            status,
            day,
            tuple(sorted(set(oracle_codes))),
            None,
            _forward_record_schema(self._historical.research_identity),
        )
        return self._append(record)

    def records(self, variant_id: ChallengerVariantId) -> tuple[ScoreR5ForwardDayRecord, ...]:
        known = {
            trade_date: record
            for (stored_variant, trade_date), record in self._records.items()
            if stored_variant == variant_id
        }
        if self._store is not None:
            for trade_date in self._historical.forward_dates:
                stored = self._store.read(variant_id, trade_date)
                if stored is not None:
                    known[trade_date] = stored
        return tuple(record for _trade_date, record in sorted(known.items()))

    def _bindings(self, variant_id: ChallengerVariantId) -> ScoreR5ForwardBindings:
        gate = next(item for item in self._historical.variants if item.variant_id == variant_id)
        if gate.state != "historical_passed":
            raise ValueError("Score-R5 forward collector requires a passed historical gate")
        return _forward_bindings(self._historical, gate)

    def _append(self, record: ScoreR5ForwardDayRecord) -> ScoreR5ForwardDayRecord:
        key = (record.bindings.variant_id, record.planned_trade_date)
        existing = self._records.get(key)
        if existing is None and self._store is not None:
            existing = self._store.read(record.bindings.variant_id, record.planned_trade_date)
        if existing is not None:
            if existing.content_hash != record.content_hash:
                raise ValueError("Score-R5 forward evidence identity conflict")
            return existing
        stored = self._store.append(record) if self._store is not None else record
        self._records[key] = stored
        return stored


class ScoreR5FinalSealer:
    """Seal fixed forward evidence without extending, replacing, or dropping planned dates."""

    def seal(
        self,
        historical: ScoreR5HistoricalReport,
        baseline: ScoreR3BaselineReport,
        challengers: ScoreR4ChallengerReport,
        records: tuple[ScoreR5ForwardDayRecord, ...],
    ) -> ScoreR5FinalReport:
        records = tuple(sorted(records, key=lambda item: (item.bindings.variant_id, item.planned_trade_date)))
        spec = get_score_research_spec(historical.research_identity)
        recalculated = ScoreR5StatisticalGate(spec).evaluate(baseline, challengers)
        if recalculated.content_hash != historical.content_hash:
            return _forward_rejected(historical, records, "historical_gate_changed")
        eligible = tuple(item for item in historical.variants if item.state == "historical_passed")
        if not eligible:
            return ScoreR5FinalReport(
                "forward_rejected",
                historical.content_hash,
                tuple(record.content_hash for record in records),
                None,
                None,
                ("no_historical_variant_passed",),
                historical.research_identity,
                historical.research_spec_hash,
                historical.report_version,
            )
        preflight = _forward_preflight(historical, eligible, records)
        if preflight == "collecting":
            return ScoreR5FinalReport(
                "forward_collecting",
                historical.content_hash,
                tuple(item.content_hash for item in records),
                None,
                None,
                (),
                historical.research_identity,
                historical.research_spec_hash,
                historical.report_version,
            )
        if preflight != "ready":
            return _forward_rejected(historical, records, preflight)
        forward_gate = _forward_gate(historical, challengers, records)
        final_gate = _combined_gate(historical, baseline, challengers, records)
        forward_by_id = {item.variant_id: item for item in forward_gate.variants}
        final_gate = replace(
            final_gate,
            variants=tuple(
                ScoreR5VariantGate(
                    item.variant_id,
                    item.variant_version,
                    "forward_rejected",
                    ("forward_gate_not_passed",),
                    item.holm,
                    item.local_track,
                    item.hybrid_track,
                    item.hybrid_increment,
                )
                if item.state == "promotion_eligible" and forward_by_id[item.variant_id].state != "forward_collecting"
                else item
                for item in final_gate.variants
            ),
        )
        if any(item.state == "promotion_eligible" for item in final_gate.variants):
            return ScoreR5FinalReport(
                "promotion_eligible",
                historical.content_hash,
                tuple(item.content_hash for item in records),
                forward_gate,
                final_gate,
                (),
                historical.research_identity,
                historical.research_spec_hash,
                historical.report_version,
            )
        return ScoreR5FinalReport(
            "forward_rejected",
            historical.content_hash,
            tuple(item.content_hash for item in records),
            forward_gate,
            final_gate,
            ("combined_gate_not_passed",),
            historical.research_identity,
            historical.research_spec_hash,
            historical.report_version,
        )


def _combined_gate(
    historical: ScoreR5HistoricalReport,
    baseline: ScoreR3BaselineReport,
    challengers: ScoreR4ChallengerReport,
    records: tuple[ScoreR5ForwardDayRecord, ...],
) -> ScoreR5HistoricalReport:
    spec = get_score_research_spec(historical.research_identity)
    engine = ScoreR5StatisticalGate(spec)
    historical_gate_by_id = {item.variant_id: item for item in historical.variants}
    historical_oracle_by_date = {item.trade_date: item.oracle_codes for item in baseline.days}
    prepared: list[ScoreR5VariantGate] = []
    eligible_ids: set[ChallengerVariantId] = set()
    for variant in challengers.variants:
        historical_gate = historical_gate_by_id[variant.variant_id]
        if historical_gate.state != "historical_passed":
            prepared.append(historical_gate)
            continue
        eligible_ids.add(variant.variant_id)
        variant_records = tuple(
            sorted(
                (item for item in records if item.bindings.variant_id == variant.variant_id),
                key=lambda item: item.planned_trade_date,
            )
        )
        forward_days = tuple(item.day for item in variant_records if item.day is not None)
        if len(forward_days) != 20:
            raise ValueError("Score-R5 combined gate requires all 20 valid forward days")
        oracle_by_date = dict(historical_oracle_by_date)
        for forward_record in variant_records:
            oracle_by_date[forward_record.planned_trade_date] = forward_record.oracle_codes
        prepared.append(
            engine._prepare_days(
                variant.variant_id,
                variant.variant_version,
                (*variant.days, *forward_days),
                oracle_by_date,
            )
        )
    p_values = {
        item.variant_id: item.local_track.primary_bootstrap.p_value if item.variant_id in eligible_ids else None
        for item in prepared
    }
    holm_by_id = {item.variant_id: item for item in holm_step_down(p_values)}
    final_variants: list[ScoreR5VariantGate] = []
    for prepared_gate in prepared:
        holm = holm_by_id[prepared_gate.variant_id]
        if prepared_gate.variant_id not in eligible_ids:
            final_variants.append(
                ScoreR5VariantGate(
                    prepared_gate.variant_id,
                    prepared_gate.variant_version,
                    "forward_rejected",
                    ("historical_gate_not_passed",),
                    holm,
                    prepared_gate.local_track,
                    prepared_gate.hybrid_track,
                    prepared_gate.hybrid_increment,
                )
            )
            continue
        failures = _gate_failures(prepared_gate.local_track, holm, 60, 60)
        final_variants.append(
            ScoreR5VariantGate(
                prepared_gate.variant_id,
                prepared_gate.variant_version,
                "forward_rejected" if failures else "promotion_eligible",
                failures,
                holm,
                prepared_gate.local_track,
                prepared_gate.hybrid_track,
                prepared_gate.hybrid_increment,
            )
        )
    return ScoreR5HistoricalReport(
        "evaluated",
        baseline.report_hash,
        challengers.content_hash,
        challengers.parameter_manifest_hash,
        60,
        tuple(final_variants),
        "combined",
        historical.forward_dates,
        historical.research_identity,
        historical.research_spec_hash,
        historical.schema_version,
        historical.statistics_version,
        historical.report_version,
    )


def _forward_gate(
    historical: ScoreR5HistoricalReport,
    challengers: ScoreR4ChallengerReport,
    records: tuple[ScoreR5ForwardDayRecord, ...],
) -> ScoreR5HistoricalReport:
    spec = get_score_research_spec(historical.research_identity)
    engine = ScoreR5StatisticalGate(spec)
    historical_gate_by_id = {item.variant_id: item for item in historical.variants}
    prepared: list[ScoreR5VariantGate] = []
    eligible_ids: set[ChallengerVariantId] = set()
    for variant in challengers.variants:
        historical_gate = historical_gate_by_id[variant.variant_id]
        if historical_gate.state != "historical_passed":
            prepared.append(historical_gate)
            continue
        eligible_ids.add(variant.variant_id)
        variant_records = tuple(
            sorted(
                (record for record in records if record.bindings.variant_id == variant.variant_id),
                key=lambda record: record.planned_trade_date,
            )
        )
        forward_days = tuple(record.day for record in variant_records if record.day is not None)
        oracle_by_date = {record.planned_trade_date: record.oracle_codes for record in variant_records}
        prepared.append(
            engine._prepare_days(
                variant.variant_id,
                variant.variant_version,
                forward_days,
                oracle_by_date,
            )
        )
    p_values = {
        gate.variant_id: gate.local_track.primary_bootstrap.p_value if gate.variant_id in eligible_ids else None
        for gate in prepared
    }
    holm_by_id = {decision.variant_id: decision for decision in holm_step_down(p_values)}
    final_variants: list[ScoreR5VariantGate] = []
    for gate in prepared:
        holm = holm_by_id[gate.variant_id]
        if gate.variant_id not in eligible_ids:
            final_variants.append(
                ScoreR5VariantGate(
                    gate.variant_id,
                    gate.variant_version,
                    "forward_rejected",
                    ("historical_gate_not_passed",),
                    holm,
                    gate.local_track,
                    gate.hybrid_track,
                    gate.hybrid_increment,
                )
            )
            continue
        failures = tuple(
            reason for reason in _gate_failures(gate.local_track, holm, 20, 20, 100) if reason != "delete_best_month"
        )
        final_variants.append(
            ScoreR5VariantGate(
                gate.variant_id,
                gate.variant_version,
                "forward_rejected" if failures else "forward_collecting",
                failures,
                holm,
                gate.local_track,
                gate.hybrid_track,
                gate.hybrid_increment,
            )
        )
    return ScoreR5HistoricalReport(
        "evaluated",
        historical.baseline_report_hash,
        challengers.content_hash,
        challengers.parameter_manifest_hash,
        20,
        tuple(final_variants),
        "forward",
        historical.forward_dates,
        historical.research_identity,
        historical.research_spec_hash,
        historical.schema_version,
        historical.statistics_version,
        historical.report_version,
    )


def _track_metrics(
    variant_id: ChallengerVariantId,
    days: tuple[ChallengerDayReplay, ...],
    oracle_by_date: dict[date, tuple[str, ...]],
    track: Literal["local_only", "hybrid"],
    spec: ScoreResearchSpec,
) -> ScoreR5TrackMetrics:
    daily = tuple(_daily_track(day, oracle_by_date[day.trade_date], track) for day in days)
    cost_means = tuple(_mean(tuple(item.cost_differences[index] for item in daily)) for index in range(3))
    severe_differences = tuple(item.challenger_severe_rate - item.baseline_severe_rate for item in daily)
    blocks = tuple(
        paired_moving_block_bootstrap(
            tuple(item.cost_differences[0] for item in daily),
            severe_differences,
            variant_id,
            block_days,
            spec=spec,
        )
        for block_days in BOOTSTRAP_BLOCK_DAYS
    )
    baseline_selected = sum(sum(pair.production_rank is not None for pair in day.pairs) for day in days)
    challenger_selected = sum(sum(_rank(pair, track) is not None for pair in day.pairs) for day in days)
    recalled = sum(item.recalled_oracle_count for item in daily)
    oracle = sum(item.oracle_count for item in daily)
    month_status, month_value = _delete_best_group(
        tuple((item.trade_date.strftime("%Y-%m"), item.cost_differences[0]) for item in daily)
    )
    board_rows = tuple(row for item in daily for row in item.board_contributions_20bp)
    board_status, board_value = _delete_best_group(board_rows)
    stock_rows = tuple(row for item in daily for row in item.stock_contributions_20bp)
    maximum_stock, top_five = _positive_concentration(stock_rows)
    high_low = tuple(item.high_minus_low_severe_rate for item in daily if item.high_minus_low_severe_rate is not None)
    high_low_ci = None
    if high_low:
        high_low_result = paired_moving_block_bootstrap(
            tuple(high_low),
            tuple(0.0 for _item in high_low),
            variant_id,
            PRIMARY_BLOCK_DAYS,
            spec=spec,
        )
        high_low_ci = high_low_result.confidence_lower
    return ScoreR5TrackMetrics(
        track,
        len(daily),
        sum(item.pair_count for item in daily),
        (cost_means[0], cost_means[1], cost_means[2]),
        blocks,
        _selected_severe_rate(days, "production", baseline_selected),
        _selected_severe_rate(days, track, challenger_selected),
        recalled / oracle if oracle else None,
        month_value,
        month_status,
        board_value,
        board_status,
        maximum_stock,
        top_five,
        _optional_mean(tuple(item.top_quintile_net_excess_20bp for item in daily)),
        _optional_mean(tuple(item.bottom_quintile_net_excess_20bp for item in daily)),
        mean_rank_ic(tuple(item.rank_ic for item in daily)),
        high_low_ci,
    )


def _daily_track(
    day: ChallengerDayReplay,
    oracle_codes: tuple[str, ...],
    track: Literal["local_only", "hybrid"],
) -> _DailyTrack:
    cost_differences = tuple(
        math.fsum(_contribution_difference(pair, track, cost) for pair in day.pairs) for cost in _COST_RATES
    )
    baseline_selected = tuple(pair for pair in day.pairs if pair.production_rank is not None)
    challenger_selected = tuple(pair for pair in day.pairs if _rank(pair, track) is not None)
    override_by_code = {item.code: item for item in day.overrides}
    recalled = sum(1 for code in oracle_codes if override_by_code[code].selection_eligible)
    stock_rows = tuple((pair.code, _contribution_difference(pair, track, _COST_RATES[0])) for pair in day.pairs)
    board_totals: dict[str, float] = defaultdict(float)
    for pair in day.pairs:
        board_totals[pair.board] += _contribution_difference(pair, track, _COST_RATES[0])
    top, bottom, rank_ic, high_low = _score_diagnostics(day, track)
    return _DailyTrack(
        day.trade_date,
        len(day.pairs),
        (cost_differences[0], cost_differences[1], cost_differences[2]),
        _severe_rate(baseline_selected),
        _severe_rate(challenger_selected),
        recalled,
        len(oracle_codes),
        stock_rows,
        tuple(sorted(board_totals.items())),
        top,
        bottom,
        rank_ic,
        high_low,
    )


def _score_diagnostics(
    day: ChallengerDayReplay,
    track: Literal["local_only", "hybrid"],
) -> tuple[float | None, float | None, float | None, float | None]:
    if len(day.pairs) < 5:
        return None, None, None, None
    score_rows = tuple((pair.code, _score(pair, track)) for pair in day.pairs)
    buckets = quantile_bucket(score_rows)
    top_pairs = tuple(pair for pair in day.pairs if buckets[pair.code] == 5)
    bottom_pairs = tuple(pair for pair in day.pairs if buckets[pair.code] == 1)
    top = _mean(tuple(pair.settlement.gross_excess_return - pair.settlement.turnover * 0.002 for pair in top_pairs))
    bottom = _mean(
        tuple(pair.settlement.gross_excess_return - pair.settlement.turnover * 0.002 for pair in bottom_pairs)
    )
    rank_ic = population_spearman(
        tuple((_score(pair, track), pair.settlement.gross_excess_return) for pair in day.pairs)
    )
    high_low = _severe_rate(top_pairs) - _severe_rate(bottom_pairs)
    return top, bottom, rank_ic, high_low


def _hybrid_increment(
    variant_id: ChallengerVariantId,
    days: tuple[ChallengerDayReplay, ...],
    spec: ScoreResearchSpec,
) -> ScoreR5HybridIncrement:
    daily_costs = tuple(
        tuple(
            math.fsum(
                _weighted(pair.hybrid_weight, pair, cost) - _weighted(pair.local_weight, pair, cost)
                for pair in day.pairs
            )
            for cost in _COST_RATES
        )
        for day in days
    )
    means = tuple(_mean(tuple(row[index] for row in daily_costs)) for index in range(3))
    blocks = tuple(
        paired_moving_block_bootstrap(
            tuple(row[0] for row in daily_costs),
            tuple(0.0 for _row in daily_costs),
            variant_id,
            block_days,
            spec=spec,
        )
        for block_days in BOOTSTRAP_BLOCK_DAYS
    )
    primary = blocks[1]
    passed = bool(
        primary.valid
        and primary.confidence_lower is not None
        and primary.confidence_lower > 0.0
        and primary.p_value is not None
        and primary.p_value <= 0.05
    )
    return ScoreR5HybridIncrement((means[0], means[1], means[2]), blocks, passed)


def _gate_failures(
    track: ScoreR5TrackMetrics,
    holm: HolmDecision,
    day_count: int,
    expected_day_count: int,
    minimum_pair_count: int = 300,
) -> tuple[str, ...]:
    primary = track.primary_bootstrap
    severe_failed = (
        track.baseline_severe_drawdown_rate is None
        or track.challenger_severe_drawdown_rate is None
        or track.challenger_severe_drawdown_rate > track.baseline_severe_drawdown_rate
    )
    concentration_failed = (
        track.maximum_stock_positive_contribution_fraction is None
        or track.maximum_stock_positive_contribution_fraction > 0.10
        or track.top_five_positive_contribution_fraction is None
        or track.top_five_positive_contribution_fraction > 0.30
    )
    quintile_failed = (
        track.top_quintile_net_excess_20bp is None
        or track.bottom_quintile_net_excess_20bp is None
        or track.top_quintile_net_excess_20bp <= track.bottom_quintile_net_excess_20bp
    )
    checks = (
        (day_count != expected_day_count, "historical_day_count"),
        (track.pair_count < minimum_pair_count, "same_stock_pair_count"),
        (not primary.valid, "primary_bootstrap_invalid"),
        (not holm.rejected_null, "holm_not_passed"),
        (primary.confidence_lower is None or primary.confidence_lower <= 0.0, "primary_confidence_lower"),
        (severe_failed, "severe_drawdown_rate"),
        (track.candidate_recall is None or track.candidate_recall < 0.99, "candidate_recall"),
        (
            track.delete_best_month_difference is None or track.delete_best_month_difference <= 0.0,
            "delete_best_month",
        ),
        (
            track.delete_best_board_difference is None or track.delete_best_board_difference < 0.0,
            "delete_best_board",
        ),
        (concentration_failed, "positive_contribution_concentration"),
        (quintile_failed, "score_quintile_order"),
        (track.mean_rank_ic is None or track.mean_rank_ic <= 0.0, "rank_ic"),
        (
            track.high_minus_low_severe_ci_lower is None or track.high_minus_low_severe_ci_lower > 0.0,
            "high_score_severe_drawdown",
        ),
    )
    return tuple(reason for failed, reason in checks if failed)


def _validate_parents(
    baseline: ScoreR3BaselineReport,
    challengers: ScoreR4ChallengerReport,
    spec: ScoreResearchSpec,
) -> None:
    if challengers.baseline_report_hash != baseline.report_hash:
        raise ValueError("Score-R5 baseline and challenger reports must share the R3 identity")
    if (
        baseline.research_identity != spec.research_identity
        or baseline.research_spec_hash != spec.content_hash
        or challengers.research_identity != spec.research_identity
        or challengers.research_spec_hash != spec.content_hash
    ):
        raise ValueError("Score-R5 parent reports must bind the selected research spec")
    baseline_dates = tuple(item.trade_date for item in baseline.days)
    if any(tuple(day.trade_date for day in variant.days) != baseline_dates for variant in challengers.variants):
        raise ValueError("Score-R5 variants must preserve the R3 historical day sequence")


def _rank(pair: ChallengerSameStockPair, track: Literal["local_only", "hybrid"]) -> int | None:
    return pair.local_rank if track == "local_only" else pair.hybrid_rank


def _score(pair: ChallengerSameStockPair, track: Literal["local_only", "hybrid"]) -> float:
    return pair.local_score if track == "local_only" else pair.hybrid_score


def _weight(pair: ChallengerSameStockPair, track: Literal["local_only", "hybrid"]) -> float:
    return pair.local_weight if track == "local_only" else pair.hybrid_weight


def _weighted(weight: float, pair: ChallengerSameStockPair, cost: float) -> float:
    settlement = pair.settlement
    return weight * (settlement.gross_excess_return - settlement.turnover * cost)


def _contribution_difference(
    pair: ChallengerSameStockPair,
    track: Literal["local_only", "hybrid"],
    cost: float,
) -> float:
    return _weighted(_weight(pair, track), pair, cost) - _weighted(pair.production_weight, pair, cost)


def _severe_rate(pairs: tuple[ChallengerSameStockPair, ...]) -> float:
    return _mean(tuple(1.0 if pair.settlement.mae_atr20 <= -1.5 else 0.0 for pair in pairs)) or 0.0


def _selected_severe_rate(
    days: tuple[ChallengerDayReplay, ...],
    track: Literal["production", "local_only", "hybrid"],
    selected_count: int,
) -> float | None:
    if not selected_count:
        return 0.0
    severe = 0
    for day in days:
        for pair in day.pairs:
            selected = pair.production_rank is not None if track == "production" else _rank(pair, track) is not None
            severe += int(selected and pair.settlement.mae_atr20 <= -1.5)
    return severe / selected_count


def _delete_best_group(
    rows: tuple[tuple[str, float], ...],
) -> tuple[Literal["deleted_positive_group", "no_positive_group", "insufficient_data"], float | None]:
    if not rows:
        return "insufficient_data", None
    grouped: dict[str, float] = defaultdict(float)
    for group, value in rows:
        grouped[group] += value
    positive = tuple((group, value) for group, value in grouped.items() if value > 0.0)
    total = math.fsum(grouped.values())
    if not positive:
        return "no_positive_group", total
    best_group, best_value = sorted(positive, key=lambda item: (-item[1], item[0]))[0]
    del best_group
    return "deleted_positive_group", total - best_value


def _positive_concentration(rows: tuple[tuple[str, float], ...]) -> tuple[float | None, float | None]:
    grouped: dict[str, float] = defaultdict(float)
    for code, value in rows:
        if value > 0.0:
            grouped[code] += value
    positive = tuple(sorted(grouped.items(), key=lambda item: (-item[1], item[0])))
    denominator = math.fsum(value for _code, value in positive)
    if denominator <= 0.0:
        return None, None
    fractions = tuple(value / denominator for _code, value in positive)
    return fractions[0], math.fsum(fractions[:5])


def _forward_preflight(
    historical: ScoreR5HistoricalReport,
    eligible: tuple[ScoreR5VariantGate, ...],
    records: tuple[ScoreR5ForwardDayRecord, ...],
) -> str:
    expected = {(gate.variant_id, day) for gate in eligible for day in historical.forward_dates}
    by_key = {(record.bindings.variant_id, record.planned_trade_date): record for record in records}
    gate_by_id = {gate.variant_id: gate for gate in eligible}
    if len(by_key) != len(records):
        result = "forward_identity_conflict"
    elif not set(by_key).issubset(expected):
        result = "unexpected_forward_identity"
    elif set(by_key) != expected:
        result = "collecting"
    elif any(record.status == "failed" for record in records):
        result = "failed_planned_day"
    elif any(
        record.bindings.content_hash
        != _forward_bindings(historical, gate_by_id[record.bindings.variant_id]).content_hash
        for record in records
    ):
        result = "forward_binding_changed"
    else:
        pair_shortage = any(
            _forward_pair_count(gate.variant_id, records) < 100
            or gate.local_track.pair_count + _forward_pair_count(gate.variant_id, records) < 300
            for gate in eligible
        )
        result = "pair_sample_shortage" if pair_shortage else "ready"
    return result


def _forward_pair_count(
    variant_id: ChallengerVariantId,
    records: tuple[ScoreR5ForwardDayRecord, ...],
) -> int:
    return sum(
        len(record.day.pairs)
        for record in records
        if record.bindings.variant_id == variant_id and record.day is not None
    )


def _forward_rejected(
    historical: ScoreR5HistoricalReport,
    records: tuple[ScoreR5ForwardDayRecord, ...],
    reason: str,
) -> ScoreR5FinalReport:
    return ScoreR5FinalReport(
        "forward_rejected",
        historical.content_hash,
        tuple(item.content_hash for item in records),
        None,
        None,
        (reason,),
        historical.research_identity,
        historical.research_spec_hash,
        historical.report_version,
    )


def _forward_bindings(
    historical: ScoreR5HistoricalReport,
    gate: ScoreR5VariantGate,
) -> ScoreR5ForwardBindings:
    return ScoreR5ForwardBindings(
        historical.content_hash,
        gate.variant_id,
        gate.variant_version,
        historical.parameter_manifest_hash,
        historical.challenger_report_hash,
        historical.parameter_manifest_hash,
        historical.baseline_report_hash,
        research_identity=historical.research_identity,
        research_spec_hash=historical.research_spec_hash,
        statistics_version=historical.statistics_version,
        report_version=historical.report_version,
    )


def _forward_record_schema(research_identity: str) -> str:
    return "score_r5_forward_day_v2" if research_identity == "score_p0_v2" else "score_r5_forward_day_v1"


def _mean(values: tuple[float, ...]) -> float | None:
    return math.fsum(values) / len(values) if values else None


def _optional_mean(values: tuple[float | None, ...]) -> float | None:
    return _mean(tuple(value for value in values if value is not None))


__all__ = ["ScoreR5FinalSealer", "ScoreR5ForwardCollector", "ScoreR5StatisticalGate"]
