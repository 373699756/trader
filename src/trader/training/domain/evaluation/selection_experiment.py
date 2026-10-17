"""Strictly paired, production-isolated candidate and selection experiments."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Literal

from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.recommendation.domain.market.factors import RoundScore
from trader.training.domain.evaluation.baseline import PopulationSpearman

SelectionStrategy = Literal["tomorrow", "d25"]
SelectionArm = Literal["rule_only", "rule_model_gate", "industry_model_utility", "pooled_model_utility"]
SelectionMode = Literal["fixed_score_top6", "gate_then_constrained_top6"]
PooledModelFamily = Literal["ridge", "lightgbm"]
SelectionTarget = Literal["tomorrow", "mean_t2_t5", "fixed_t5", "predefined_exit", "multi_horizon"]
SelectionEvidenceTarget = Literal["t1", "t2", "t3", "t4", "t5", "predefined_exit"]
SelectionExperimentStatus = Literal["historical_data_insufficient", "historical_experiment_completed"]

SELECTION_ARMS: tuple[SelectionArm, ...] = (
    "rule_only",
    "rule_model_gate",
    "industry_model_utility",
    "pooled_model_utility",
)
SELECTION_MODES: tuple[SelectionMode, ...] = ("fixed_score_top6", "gate_then_constrained_top6")
SELECTION_CAPACITIES: tuple[int | None, ...] = (60, 120, 180, 240, None)
D25_SELECTION_TARGETS: tuple[SelectionTarget, ...] = (
    "mean_t2_t5",
    "fixed_t5",
    "predefined_exit",
    "multi_horizon",
)
SELECTION_COST_BPS = (20, 50, 100)
_BOARDS = {"main", "chinext", "star"}
_HASH = re.compile(r"^[0-9a-f]{64}$")
_MODEL_ID = re.compile(r"^[a-z0-9_]{1,96}$")
_REASON = re.compile(r"^[a-z0-9_]{1,96}$")
_D25_HORIZONS: tuple[Literal["t2", "t3", "t4", "t5"], ...] = ("t2", "t3", "t4", "t5")


@dataclass(frozen=True)
class SelectionTargetEvidence:
    """Predictions and matured outcome for one preregistered realizable target."""

    target: SelectionEvidenceTarget
    industry_prediction_pre_cost: float
    pooled_ridge_prediction_pre_cost: float
    pooled_lightgbm_prediction_pre_cost: float
    actual_pre_cost_excess_return: float
    exit_untradable: bool

    def __post_init__(self) -> None:
        values = (
            self.industry_prediction_pre_cost,
            self.pooled_ridge_prediction_pre_cost,
            self.pooled_lightgbm_prediction_pre_cost,
            self.actual_pre_cost_excess_return,
        )
        if self.target not in {"t1", "t2", "t3", "t4", "t5", "predefined_exit"}:
            raise ValueError("selection target evidence target is invalid")
        if any(not math.isfinite(value) for value in values):
            raise ValueError("selection target evidence values must be finite")


@dataclass(frozen=True)
class PairedSelectionRow:
    """One immutable point-in-time row shared unchanged by every experiment arm."""

    trade_date: date
    code: str
    board: Literal["main", "chinext", "star"]
    industry: str
    parent_row_hash: str
    candidate_rank_by_board: int
    rule_score: float
    estimated_round_trip_cost: float
    risk_eligible: bool
    action_eligible: bool
    capacity_sufficient: bool
    execution_capacity: float
    severe_loss: bool
    mae_atr20: float
    target_evidence: tuple[SelectionTargetEvidence, ...]
    point_in_time_parity: bool

    def __post_init__(self) -> None:
        evidence = tuple(self.target_evidence)
        if (
            len(self.code) != 6
            or not self.code.isdigit()
            or self.board not in _BOARDS
            or not self.industry.strip()
            or _HASH.fullmatch(self.parent_row_hash) is None
            or self.candidate_rank_by_board < 1
        ):
            raise ValueError("paired selection row identity is invalid")
        if any(
            not math.isfinite(value)
            for value in (self.rule_score, self.estimated_round_trip_cost, self.execution_capacity, self.mae_atr20)
        ):
            raise ValueError("paired selection row values must be finite")
        if self.estimated_round_trip_cost < 0.0 or self.execution_capacity < 0.0 or self.mae_atr20 > 0.0:
            raise ValueError("paired selection cost, capacity or MAE is invalid")
        targets = tuple(item.target for item in evidence)
        if len(set(targets)) != len(targets):
            raise ValueError("paired selection target evidence must be unique")
        object.__setattr__(self, "target_evidence", evidence)


@dataclass(frozen=True)
class PairedSelectionSpecification:
    """Frozen experiment grid created before any evaluation outcome is read."""

    strategy: SelectionStrategy
    dataset_hash: str
    dataset_manifest_hash: str
    candidate_recall_hash: str
    industry_model_id: str
    industry_model_hash: str
    pooled_ridge_model_id: str
    pooled_ridge_model_hash: str
    pooled_lightgbm_model_id: str
    pooled_lightgbm_model_hash: str
    research_dates: tuple[date, ...]
    predefined_exit_identity: str
    pooled_exposures: tuple[str, ...] = ("market", "board", "industry", "log_average_amount_20d")
    capacities: tuple[int | None, ...] = SELECTION_CAPACITIES
    selection_modes: tuple[SelectionMode, ...] = SELECTION_MODES
    cost_bps: tuple[int, int, int] = SELECTION_COST_BPS
    top_k: int = 6
    maximum_board_count: int = 4
    maximum_industry_count: int = 2
    terminal_holdout_opened: bool = False
    production_authority: bool = False
    schema_version: str = "paired_selection_experiment"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        dates = tuple(sorted(set(self.research_dates)))
        hashes = (
            self.dataset_hash,
            self.dataset_manifest_hash,
            self.candidate_recall_hash,
            self.industry_model_hash,
            self.pooled_ridge_model_hash,
            self.pooled_lightgbm_model_hash,
        )
        model_ids = (self.industry_model_id, self.pooled_ridge_model_id, self.pooled_lightgbm_model_id)
        if self.strategy not in {"tomorrow", "d25"} or any(_HASH.fullmatch(value) is None for value in hashes):
            raise ValueError("paired selection specification identity is invalid")
        if any(_MODEL_ID.fullmatch(value) is None for value in model_ids) or not dates:
            raise ValueError("paired selection model identity or date set is invalid")
        if len(set(model_ids)) != len(model_ids) or len(set(hashes[3:])) != len(hashes[3:]):
            raise ValueError("paired selection models must have distinct identities")
        if self.strategy == "d25" and not self.predefined_exit_identity.strip():
            raise ValueError("D25 predefined exit identity is required")
        if self.capacities != SELECTION_CAPACITIES or self.selection_modes != SELECTION_MODES:
            raise ValueError("paired selection grid must use the preregistered capacities and modes")
        if self.cost_bps != SELECTION_COST_BPS or self.top_k != 6:
            raise ValueError("paired selection costs and TopK are fixed")
        if self.maximum_board_count != 4 or self.maximum_industry_count != 2:
            raise ValueError("paired selection concentration constraints are fixed")
        if self.pooled_exposures != ("market", "board", "industry", "log_average_amount_20d"):
            raise ValueError("pooled selection models require market, board, industry and liquidity exposures")
        if self.terminal_holdout_opened or self.production_authority:
            raise ValueError("paired selection research cannot open terminal data or authorize production")
        object.__setattr__(self, "research_dates", dates)
        object.__setattr__(self, "content_hash", CanonicalDigest(self))


@dataclass(frozen=True)
class SelectionExperimentVariant:
    arm: SelectionArm
    capacity_per_board: int | None
    selection_mode: SelectionMode
    target: SelectionTarget
    pooled_model_family: PooledModelFamily | None

    def __post_init__(self) -> None:
        if self.arm not in SELECTION_ARMS or self.capacity_per_board not in SELECTION_CAPACITIES:
            raise ValueError("selection experiment arm or capacity is invalid")
        if self.selection_mode not in SELECTION_MODES:
            raise ValueError("selection experiment mode is invalid")
        if (self.arm == "pooled_model_utility") != (self.pooled_model_family is not None):
            raise ValueError("pooled model family must be present only for the pooled arm")
        if self.pooled_model_family not in {None, "ridge", "lightgbm"}:
            raise ValueError("selection experiment pooled model family is invalid")


@dataclass(frozen=True)
class SelectionExperimentMetrics:
    evaluated_trade_dates: int
    population_rows: int
    candidate_rows: int
    selected_rows: int
    empty_portfolio_days: int
    mean_six_slot_net_excess_returns: tuple[float, float, float]
    paired_control_increments: tuple[float, float, float]
    rank_ic: float | None
    ndcg_at_6: float | None
    win_rate_20bp: float
    maximum_drawdown_20bp: float
    turnover: float
    severe_loss_rate: float | None
    mean_mae_atr20: float | None
    exit_untradable_rate: float | None
    mean_execution_capacity: float
    capacity_shortfall_rate: float | None
    candidate_oracle_top6_recall: float
    selected_oracle_top6_recall: float
    horizon_mean_net_excess_returns_20bp: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        counts = (
            self.evaluated_trade_dates,
            self.population_rows,
            self.candidate_rows,
            self.selected_rows,
            self.empty_portfolio_days,
        )
        if any(value < 0 for value in counts):
            raise ValueError("selection experiment counts cannot be negative")
        if (
            self.selected_rows > self.evaluated_trade_dates * 6
            or self.empty_portfolio_days > self.evaluated_trade_dates
            or self.candidate_rows > self.population_rows
        ):
            raise ValueError("selection experiment count relationships are invalid")
        if any(len(values) != 3 for values in (self.mean_six_slot_net_excess_returns, self.paired_control_increments)):
            raise ValueError("selection experiment requires 20bp, 50bp and 100bp metrics")
        values = (
            *self.mean_six_slot_net_excess_returns,
            *self.paired_control_increments,
            self.win_rate_20bp,
            self.maximum_drawdown_20bp,
            self.turnover,
            self.mean_execution_capacity,
            self.candidate_oracle_top6_recall,
            self.selected_oracle_top6_recall,
            *self.horizon_mean_net_excess_returns_20bp,
        )
        optional = (
            self.rank_ic,
            self.ndcg_at_6,
            self.severe_loss_rate,
            self.mean_mae_atr20,
            self.exit_untradable_rate,
            self.capacity_shortfall_rate,
        )
        if any(not math.isfinite(value) for value in values) or any(
            value is not None and not math.isfinite(value) for value in optional
        ):
            raise ValueError("selection experiment metrics must be finite")
        rates = (
            self.win_rate_20bp,
            self.turnover,
            self.ndcg_at_6,
            self.severe_loss_rate,
            self.exit_untradable_rate,
            self.capacity_shortfall_rate,
            self.candidate_oracle_top6_recall,
            self.selected_oracle_top6_recall,
        )
        if any(value is not None and not 0.0 <= value <= 1.0 for value in rates):
            raise ValueError("selection experiment rates must be in [0, 1]")
        if self.mean_mae_atr20 is not None and self.mean_mae_atr20 > 0.0:
            raise ValueError("selection experiment MAE must be non-positive")
        if self.maximum_drawdown_20bp < 0.0 or self.mean_execution_capacity < 0.0:
            raise ValueError("selection experiment drawdown and capacity must be non-negative")


@dataclass(frozen=True)
class SelectionExperimentResult:
    variant: SelectionExperimentVariant
    metrics: SelectionExperimentMetrics


@dataclass(frozen=True)
class PairedSelectionExperimentReport:
    specification_hash: str
    dataset_hash: str
    dataset_manifest_hash: str
    candidate_recall_hash: str
    strategy: SelectionStrategy
    status: SelectionExperimentStatus
    results: tuple[SelectionExperimentResult, ...]
    failure_reasons: tuple[str, ...]
    terminal_holdout_opened: bool = False
    production_authority: bool = False
    schema_version: str = "paired_selection_experiment_report"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        reasons = tuple(sorted(set(self.failure_reasons)))
        if self.strategy not in {"tomorrow", "d25"} or self.status not in {
            "historical_data_insufficient",
            "historical_experiment_completed",
        }:
            raise ValueError("paired selection report strategy or status is invalid")
        if any(
            _HASH.fullmatch(value) is None
            for value in (
                self.specification_hash,
                self.dataset_hash,
                self.dataset_manifest_hash,
                self.candidate_recall_hash,
            )
        ):
            raise ValueError("paired selection report identity is invalid")
        if any(_REASON.fullmatch(value) is None for value in reasons):
            raise ValueError("paired selection report failure reasons are invalid")
        if self.status == "historical_experiment_completed" and (reasons or not self.results):
            raise ValueError("completed paired selection report must contain results without failures")
        if self.status == "historical_data_insufficient" and (not reasons or self.results):
            raise ValueError("insufficient paired selection report must contain only failures")
        if self.terminal_holdout_opened or self.production_authority:
            raise ValueError("paired selection report cannot open terminal data or authorize production")
        result_keys = tuple(
            (
                item.variant.arm,
                item.variant.capacity_per_board,
                item.variant.selection_mode,
                item.variant.target,
                item.variant.pooled_model_family,
            )
            for item in self.results
        )
        expected_targets = set(D25_SELECTION_TARGETS if self.strategy == "d25" else ("tomorrow",))
        expected_count = len(SELECTION_CAPACITIES) * len(SELECTION_MODES) * len(expected_targets) * 5
        if self.results and (
            len(result_keys) != expected_count
            or len(set(result_keys)) != len(result_keys)
            or {item.variant.target for item in self.results} != expected_targets
        ):
            raise ValueError("paired selection report does not contain the complete preregistered grid")
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "results", tuple(self.results))
        object.__setattr__(self, "content_hash", CanonicalDigest(self))


def InsufficientPairedSelectionExperimentReport(
    specification: PairedSelectionSpecification, reasons: tuple[str, ...]
) -> PairedSelectionExperimentReport:
    return PairedSelectionExperimentReport(
        specification.content_hash,
        specification.dataset_hash,
        specification.dataset_manifest_hash,
        specification.candidate_recall_hash,
        specification.strategy,
        "historical_data_insufficient",
        (),
        reasons,
    )


def EvaluatePairedSelectionExperiment(
    specification: PairedSelectionSpecification, rows: tuple[PairedSelectionRow, ...]
) -> PairedSelectionExperimentReport:
    """Evaluate every preregistered arm on exactly the same rows and dates."""

    ordered = tuple(
        sorted(rows, key=lambda item: (item.trade_date, item.board, item.candidate_rank_by_board, item.code))
    )
    _validate_rows(specification, ordered)
    targets: tuple[SelectionTarget, ...] = D25_SELECTION_TARGETS if specification.strategy == "d25" else ("tomorrow",)
    results_by_key: dict[tuple[int | None, SelectionMode, SelectionTarget], SelectionExperimentMetrics] = {}
    results: list[SelectionExperimentResult] = []
    for capacity in specification.capacities:
        for mode in specification.selection_modes:
            for target in targets:
                for arm in SELECTION_ARMS:
                    families: tuple[PooledModelFamily | None, ...] = (
                        ("ridge", "lightgbm") if arm == "pooled_model_utility" else (None,)
                    )
                    for family in families:
                        variant = SelectionExperimentVariant(arm, capacity, mode, target, family)
                        metrics = _evaluate_variant(specification, ordered, variant)
                        control_key = (capacity, mode, target)
                        if arm == "rule_only":
                            results_by_key[control_key] = metrics
                        else:
                            control = results_by_key[control_key]
                            metrics = _with_control_increment(metrics, control)
                        results.append(SelectionExperimentResult(variant, metrics))
    return PairedSelectionExperimentReport(
        specification.content_hash,
        specification.dataset_hash,
        specification.dataset_manifest_hash,
        specification.candidate_recall_hash,
        specification.strategy,
        "historical_experiment_completed",
        tuple(results),
        (),
    )


def _validate_rows(specification: PairedSelectionSpecification, rows: tuple[PairedSelectionRow, ...]) -> None:
    if not rows or any(not row.point_in_time_parity for row in rows):
        raise ValueError("paired selection rows require point-in-time parity")
    if tuple(sorted({row.trade_date for row in rows})) != specification.research_dates:
        raise ValueError("paired selection rows do not match the preregistered date set")
    if len({(row.trade_date, row.code) for row in rows}) != len(rows):
        raise ValueError("paired selection rows must be unique by date and code")
    for row in rows:
        targets = tuple(item.target for item in row.target_evidence)
        expected = ("t1",) if specification.strategy == "tomorrow" else (*_D25_HORIZONS, "predefined_exit")
        if targets != expected:
            raise ValueError("paired selection target evidence does not match the strategy")
    grouped: dict[tuple[date, str], list[int]] = defaultdict(list)
    for row in rows:
        grouped[(row.trade_date, row.board)].append(row.candidate_rank_by_board)
    if any(tuple(sorted(ranks)) != tuple(range(1, len(ranks) + 1)) for ranks in grouped.values()):
        raise ValueError("paired selection board candidate ranks must be contiguous")


@dataclass
class _SelectionAccumulator:
    daily_returns: list[tuple[float, float, float]] = field(default_factory=list)
    selection_sets: list[frozenset[str]] = field(default_factory=list)
    rank_ics: list[float] = field(default_factory=list)
    ndcgs: list[float] = field(default_factory=list)
    severe: list[float] = field(default_factory=list)
    maes: list[float] = field(default_factory=list)
    untradable: list[float] = field(default_factory=list)
    capacities: list[float] = field(default_factory=list)
    shortfalls: list[float] = field(default_factory=list)
    candidate_oracle_hits: int = 0
    selected_oracle_hits: int = 0
    oracle_total: int = 0
    candidate_rows: int = 0
    selected_rows: int = 0
    empty_days: int = 0
    horizon_daily: list[tuple[float, float, float, float]] = field(default_factory=list)


def _evaluate_variant(
    specification: PairedSelectionSpecification,
    rows: tuple[PairedSelectionRow, ...],
    variant: SelectionExperimentVariant,
) -> SelectionExperimentMetrics:
    grouped: dict[date, list[PairedSelectionRow]] = defaultdict(list)
    for row in rows:
        grouped[row.trade_date].append(row)
    accumulator = _SelectionAccumulator()
    for trade_date in specification.research_dates:
        day = tuple(grouped[trade_date])
        candidates = tuple(
            row
            for row in day
            if variant.capacity_per_board is None or row.candidate_rank_by_board <= variant.capacity_per_board
        )
        selected = _select(candidates, variant, specification)
        _accumulate_day(accumulator, day, candidates, selected, variant)
    means = _mean_costs(accumulator.daily_returns)
    return SelectionExperimentMetrics(
        len(grouped),
        len(rows),
        accumulator.candidate_rows,
        accumulator.selected_rows,
        accumulator.empty_days,
        means,
        (0.0, 0.0, 0.0),
        _mean(tuple(accumulator.rank_ics)) if accumulator.rank_ics else None,
        _mean(tuple(accumulator.ndcgs)) if accumulator.ndcgs else None,
        sum(item[0] > 0.0 for item in accumulator.daily_returns) / len(accumulator.daily_returns),
        _maximum_drawdown(tuple(item[0] for item in accumulator.daily_returns)),
        _turnover(accumulator.selection_sets),
        _mean(tuple(accumulator.severe)) if accumulator.severe else None,
        _mean(tuple(accumulator.maes)) if accumulator.maes else None,
        _mean(tuple(accumulator.untradable)) if accumulator.untradable else None,
        _mean(tuple(accumulator.capacities)),
        _mean(tuple(accumulator.shortfalls)) if accumulator.shortfalls else None,
        accumulator.candidate_oracle_hits / accumulator.oracle_total,
        accumulator.selected_oracle_hits / accumulator.oracle_total,
        tuple(_mean(tuple(day[index] for day in accumulator.horizon_daily)) for index in range(4))
        if accumulator.horizon_daily
        else (),
    )


def _select(
    candidates: tuple[PairedSelectionRow, ...],
    variant: SelectionExperimentVariant,
    specification: PairedSelectionSpecification,
) -> tuple[PairedSelectionRow, ...]:
    ordered = tuple(sorted(candidates, key=lambda row: (-_ranking_value(row, variant), row.code)))
    if variant.selection_mode == "fixed_score_top6":
        leaders = ordered[: specification.top_k]
        eligible_leaders = tuple(row for row in leaders if _passes_gates(row, variant))
        return _constrained_top_k(eligible_leaders, specification)
    eligible = tuple(row for row in ordered if _passes_gates(row, variant))
    return _constrained_top_k(eligible, specification)


def _constrained_top_k(
    rows: tuple[PairedSelectionRow, ...], specification: PairedSelectionSpecification
) -> tuple[PairedSelectionRow, ...]:
    selected: list[PairedSelectionRow] = []
    board_counts: dict[str, int] = defaultdict(int)
    industry_counts: dict[str, int] = defaultdict(int)
    for row in rows:
        if board_counts[row.board] >= specification.maximum_board_count:
            continue
        if industry_counts[row.industry] >= specification.maximum_industry_count:
            continue
        selected.append(row)
        board_counts[row.board] += 1
        industry_counts[row.industry] += 1
        if len(selected) == specification.top_k:
            break
    return tuple(selected)


def _passes_gates(row: PairedSelectionRow, variant: SelectionExperimentVariant) -> bool:
    if not (row.risk_eligible and row.action_eligible and row.capacity_sufficient):
        return False
    if variant.arm == "rule_only":
        return True
    return _model_utility(row, variant) > 0.0


def _ranking_value(row: PairedSelectionRow, variant: SelectionExperimentVariant) -> float:
    if variant.arm in {"rule_only", "rule_model_gate"}:
        return RoundScore(row.rule_score)
    return _model_utility(row, variant)


def _model_utility(row: PairedSelectionRow, variant: SelectionExperimentVariant) -> float:
    predictions = _target_predictions(row, variant)
    if variant.target == "multi_horizon":
        return min(predictions) - row.estimated_round_trip_cost
    return predictions[0] - row.estimated_round_trip_cost


def _target_predictions(row: PairedSelectionRow, variant: SelectionExperimentVariant) -> tuple[float, ...]:
    evidence = {item.target: item for item in row.target_evidence}
    values = _selected_prediction_values(evidence, variant)
    if variant.target == "tomorrow":
        return (values["t1"],)
    if variant.target == "fixed_t5":
        return (values["t5"],)
    if variant.target == "predefined_exit":
        return (values["predefined_exit"],)
    horizons = tuple(values[target] for target in _D25_HORIZONS)
    return horizons if variant.target == "multi_horizon" else (_mean(horizons),)


def _selected_prediction_values(
    evidence: Mapping[SelectionEvidenceTarget, SelectionTargetEvidence], variant: SelectionExperimentVariant
) -> dict[SelectionEvidenceTarget, float]:
    if variant.arm in {"rule_model_gate", "industry_model_utility"}:
        return {target: item.industry_prediction_pre_cost for target, item in evidence.items()}
    if variant.pooled_model_family == "ridge":
        return {target: item.pooled_ridge_prediction_pre_cost for target, item in evidence.items()}
    return {target: item.pooled_lightgbm_prediction_pre_cost for target, item in evidence.items()}


def _target_actual(row: PairedSelectionRow, target: SelectionTarget) -> float:
    evidence = {item.target: item for item in row.target_evidence}
    if target == "tomorrow":
        return evidence["t1"].actual_pre_cost_excess_return
    if target == "fixed_t5":
        return evidence["t5"].actual_pre_cost_excess_return
    if target == "predefined_exit":
        return evidence["predefined_exit"].actual_pre_cost_excess_return
    return _mean(tuple(evidence[name].actual_pre_cost_excess_return for name in _D25_HORIZONS))


def _target_untradable(row: PairedSelectionRow, target: SelectionTarget) -> bool:
    evidence = {item.target: item for item in row.target_evidence}
    if target == "tomorrow":
        return evidence["t1"].exit_untradable
    if target == "fixed_t5":
        return evidence["t5"].exit_untradable
    if target == "predefined_exit":
        return evidence["predefined_exit"].exit_untradable
    return any(evidence[name].exit_untradable for name in _D25_HORIZONS)


def _accumulate_day(
    accumulator: _SelectionAccumulator,
    population: tuple[PairedSelectionRow, ...],
    candidates: tuple[PairedSelectionRow, ...],
    selected: tuple[PairedSelectionRow, ...],
    variant: SelectionExperimentVariant,
) -> None:
    costs = tuple(value / 10_000.0 for value in SELECTION_COST_BPS)
    accumulator.daily_returns.append(
        tuple(math.fsum(_target_actual(row, variant.target) - cost for row in selected) / 6.0 for cost in costs)  # type: ignore[arg-type]
    )
    accumulator.selection_sets.append(frozenset(row.code for row in selected))
    accumulator.candidate_rows += len(candidates)
    accumulator.selected_rows += len(selected)
    accumulator.empty_days += not selected
    accumulator.shortfalls.extend(float(not row.capacity_sufficient) for row in candidates)
    actuals = {row.code: _target_actual(row, variant.target) for row in population}
    oracle = tuple(row.code for row in sorted(population, key=lambda row: (-actuals[row.code], row.code))[:6])
    candidate_codes = {row.code for row in candidates}
    selected_codes = {row.code for row in selected}
    accumulator.oracle_total += len(oracle)
    accumulator.candidate_oracle_hits += sum(code in candidate_codes for code in oracle)
    accumulator.selected_oracle_hits += sum(code in selected_codes for code in oracle)
    pairs = tuple((_ranking_value(row, variant), actuals[row.code]) for row in candidates)
    rank_ic = PopulationSpearman(pairs)
    if rank_ic is not None:
        accumulator.rank_ics.append(rank_ic)
    ndcg = _ndcg_at_6(candidates, variant)
    if ndcg is not None:
        accumulator.ndcgs.append(ndcg)
    if selected:
        accumulator.severe.append(sum(row.severe_loss for row in selected) / len(selected))
        accumulator.maes.extend(row.mae_atr20 for row in selected)
        accumulator.untradable.extend(float(_target_untradable(row, variant.target)) for row in selected)
    accumulator.capacities.append(math.fsum(row.execution_capacity for row in selected))
    if variant.target in {"mean_t2_t5", "multi_horizon"}:
        evidence = tuple({item.target: item for item in row.target_evidence} for row in selected)
        accumulator.horizon_daily.append(
            tuple(
                math.fsum(items[target].actual_pre_cost_excess_return - costs[0] for items in evidence) / 6.0
                for target in _D25_HORIZONS
            )  # type: ignore[arg-type]
        )


def _ndcg_at_6(candidates: tuple[PairedSelectionRow, ...], variant: SelectionExperimentVariant) -> float | None:
    if not candidates:
        return None
    actual = {row.code: _target_actual(row, variant.target) for row in candidates}
    shift = min(0.0, min(actual.values()))
    ordered = sorted(candidates, key=lambda row: (-_ranking_value(row, variant), row.code))[:6]
    ideal = sorted(candidates, key=lambda row: (-actual[row.code], row.code))[:6]

    def discounted(rows: list[PairedSelectionRow]) -> float:
        return math.fsum((actual[row.code] - shift) / math.log2(index + 2.0) for index, row in enumerate(rows))

    ideal_value = discounted(ideal)
    return discounted(ordered) / ideal_value if ideal_value > 0.0 else None


def _with_control_increment(
    metrics: SelectionExperimentMetrics, control: SelectionExperimentMetrics
) -> SelectionExperimentMetrics:
    increments = (
        metrics.mean_six_slot_net_excess_returns[0] - control.mean_six_slot_net_excess_returns[0],
        metrics.mean_six_slot_net_excess_returns[1] - control.mean_six_slot_net_excess_returns[1],
        metrics.mean_six_slot_net_excess_returns[2] - control.mean_six_slot_net_excess_returns[2],
    )
    return replace(metrics, paired_control_increments=increments)


def _mean_costs(values: list[tuple[float, float, float]]) -> tuple[float, float, float]:
    return tuple(_mean(tuple(item[index] for item in values)) for index in range(3))  # type: ignore[return-value]


def _maximum_drawdown(values: tuple[float, ...]) -> float:
    cumulative = 0.0
    peak = 0.0
    maximum = 0.0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        maximum = max(maximum, peak - cumulative)
    return maximum


def _turnover(sets: list[frozenset[str]]) -> float:
    previous: frozenset[str] = frozenset()
    values: list[float] = []
    for current in sets:
        values.append(len(current - previous) / 6.0)
        previous = current
    return _mean(tuple(values))


def _mean(values: tuple[float, ...]) -> float:
    return math.fsum(values) / len(values) if values else 0.0


__all__ = [
    "D25_SELECTION_TARGETS",
    "SELECTION_ARMS",
    "SELECTION_CAPACITIES",
    "SELECTION_COST_BPS",
    "SELECTION_MODES",
    "EvaluatePairedSelectionExperiment",
    "InsufficientPairedSelectionExperimentReport",
    "PairedSelectionExperimentReport",
    "PairedSelectionRow",
    "PairedSelectionSpecification",
    "PooledModelFamily",
    "SelectionArm",
    "SelectionExperimentMetrics",
    "SelectionExperimentResult",
    "SelectionExperimentStatus",
    "SelectionExperimentVariant",
    "SelectionMode",
    "SelectionEvidenceTarget",
    "SelectionStrategy",
    "SelectionTarget",
    "SelectionTargetEvidence",
]
