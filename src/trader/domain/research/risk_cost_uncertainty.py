"""Production-isolated risk, cost, uncertainty, and DeepSeek ablation."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from statistics import fmean
from typing import Literal

from trader.domain.market.factors import clamp, round_score
from trader.domain.recommendation.risk_fusion.decision import RiskDecision
from trader.domain.recommendation.scoring.alpha import AlphaScore
from trader.domain.recommendation.selection.execution_cost import ExecutionCost
from trader.domain.research.historical import SUPPORTED_RESEARCH_BOARDS, ResearchBoard

DeepSeekResearchOutcome = Literal["applied", "failed", "late", "budget_exhausted", "abstained"]
DeepSeekAblationArm = Literal["local_only", "structured_facts_veto", "fixed_68_32"]
RiskCostUncertaintyStatus = Literal["evaluated", "historical_data_insufficient", "historical_rejected"]
UncertaintyDimension = Literal[
    "model_disagreement",
    "training_window_disagreement",
    "ood_distance",
    "missing_uncertainty",
]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FACT_ID = re.compile(r"^[a-z0-9_]{1,96}$")
_REASON_ID = re.compile(r"^[a-z0-9_]{1,96}$")
_ARMS: tuple[DeepSeekAblationArm, ...] = ("local_only", "structured_facts_veto", "fixed_68_32")
_TOP_K = 6
_MAX_PER_INDUSTRY = 2
_MAX_PER_BOARD = 3


@dataclass(frozen=True)
class DeepSeekResearchReview:
    outcome: DeepSeekResearchOutcome
    score: float | None
    structured_risk_penalty: float
    structured_veto: bool
    structured_fact_ids: tuple[str, ...]
    narrative: str = field(compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.outcome not in {"applied", "failed", "late", "budget_exhausted", "abstained"}:
            raise ValueError("DeepSeek research outcome is invalid")
        facts = tuple(sorted(set(self.structured_fact_ids)))
        if len(facts) != len(self.structured_fact_ids) or any(_FACT_ID.fullmatch(item) is None for item in facts):
            raise ValueError("DeepSeek structured fact identities are invalid")
        if not isinstance(self.narrative, str):
            raise ValueError("DeepSeek narrative must be text")
        if self.outcome != "applied":
            if self.score is not None or self.structured_risk_penalty != 0.0 or self.structured_veto or facts:
                raise ValueError("non-applied DeepSeek review must preserve local without risk effects")
        else:
            if self.score is None or not math.isfinite(self.score) or not 0.0 <= self.score <= 100.0:
                raise ValueError("applied DeepSeek score must be in [0, 100]")
            if not math.isfinite(self.structured_risk_penalty) or not 0.0 <= self.structured_risk_penalty <= 30.0:
                raise ValueError("DeepSeek structured risk penalty is invalid")
            if (self.structured_risk_penalty > 0.0 or self.structured_veto) and not facts:
                raise ValueError("DeepSeek risk effects require structured facts")
        object.__setattr__(self, "structured_fact_ids", facts)


@dataclass(frozen=True)
class RiskCostUncertaintySample:
    trade_date: date
    board: ResearchBoard
    industry: str
    alpha: AlphaScore
    risk: RiskDecision
    cost: ExecutionCost
    deepseek: DeepSeekResearchReview
    actual_alpha_return: float
    actual_net_excess_return: float
    actual_severe_loss: bool

    def __post_init__(self) -> None:
        if self.board not in SUPPORTED_RESEARCH_BOARDS or not self.industry.strip():
            raise ValueError("risk-cost sample exposure identity is invalid")
        if len({self.alpha.code, self.risk.code, self.cost.code}) != 1:
            raise ValueError("risk-cost sample component codes must match")
        if not math.isfinite(self.actual_alpha_return) or not math.isfinite(self.actual_net_excess_return):
            raise ValueError("risk-cost sample outcomes must be finite")
        expected_net = self.actual_alpha_return - self.cost.estimated_round_trip_return
        if not math.isclose(self.actual_net_excess_return, expected_net, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("risk-cost sample must deduct round-trip cost exactly once")
        object.__setattr__(self, "industry", self.industry.strip())

    @property
    def code(self) -> str:
        return self.alpha.code


@dataclass(frozen=True)
class SelectionUtility:
    code: str
    local_score: float
    expected_net_excess_return: float
    severe_loss_probability: float | None
    uncertainty_tiebreak: float
    eligible: bool

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit():
            raise ValueError("selection utility code must contain exactly six digits")
        if (
            not math.isfinite(self.local_score)
            or not 0.0 <= self.local_score <= 100.0
            or not math.isfinite(self.expected_net_excess_return)
            or not math.isfinite(self.uncertainty_tiebreak)
            or self.uncertainty_tiebreak < 0.0
        ):
            raise ValueError("selection utility values are invalid")
        if self.severe_loss_probability is not None and (
            not math.isfinite(self.severe_loss_probability) or not 0.0 <= self.severe_loss_probability <= 1.0
        ):
            raise ValueError("selection utility severe-loss probability is invalid")


def build_selection_utility(alpha: AlphaScore, risk: RiskDecision, cost: ExecutionCost) -> SelectionUtility:
    if len({alpha.code, risk.code, cost.code}) != 1:
        raise ValueError("selection utility component codes must match")
    uncertainty_values = (
        risk.uncertainty.model_disagreement,
        risk.uncertainty.training_window_disagreement,
        risk.uncertainty.ood_distance,
        risk.uncertainty.missing_uncertainty,
    )
    uncertainty = math.fsum(value for value in uncertainty_values if value is not None)
    net = alpha.predicted_excess_return - cost.estimated_round_trip_return
    return SelectionUtility(
        alpha.code,
        round_score(clamp(alpha.signal_score - risk.penalty_points)),
        net,
        risk.uncertainty.severe_loss_probability,
        uncertainty,
        not risk.veto and net > 0.0,
    )


@dataclass(frozen=True)
class UncertaintyBucketMetrics:
    dimension: UncertaintyDimension
    bucket_index: int
    sample_count: int
    lower_value: float
    upper_value: float
    mean_net_excess_return: float
    severe_loss_rate: float

    def __post_init__(self) -> None:
        if self.dimension not in {
            "model_disagreement",
            "training_window_disagreement",
            "ood_distance",
            "missing_uncertainty",
        }:
            raise ValueError("uncertainty bucket dimension is invalid")
        if not 1 <= self.bucket_index <= 3 or self.sample_count < 1:
            raise ValueError("uncertainty bucket identity is invalid")
        if (
            not math.isfinite(self.lower_value)
            or not math.isfinite(self.upper_value)
            or self.lower_value > self.upper_value
            or not math.isfinite(self.mean_net_excess_return)
            or not math.isfinite(self.severe_loss_rate)
            or not 0.0 <= self.severe_loss_rate <= 1.0
        ):
            raise ValueError("uncertainty bucket metric is invalid")


@dataclass(frozen=True)
class UncertaintyCalibrationMetrics:
    sample_count: int
    probability_count: int
    brier_score: float | None
    expected_calibration_error: float | None
    probability_coverage: float
    prediction_interval_coverage: float | None
    prediction_interval_availability: float
    model_disagreement_coverage: float
    training_window_disagreement_coverage: float
    ood_coverage: float
    missing_uncertainty_coverage: float
    uncertainty_buckets: tuple[UncertaintyBucketMetrics, ...]

    def __post_init__(self) -> None:
        if self.sample_count < 1 or not 0 <= self.probability_count <= self.sample_count:
            raise ValueError("uncertainty calibration counts are invalid")
        optional_rates = (
            self.brier_score,
            self.expected_calibration_error,
            self.prediction_interval_coverage,
        )
        if any(value is not None and (not math.isfinite(value) or not 0.0 <= value <= 1.0) for value in optional_rates):
            raise ValueError("uncertainty calibration metric is invalid")
        rates = (
            self.probability_coverage,
            self.prediction_interval_availability,
            self.model_disagreement_coverage,
            self.training_window_disagreement_coverage,
            self.ood_coverage,
            self.missing_uncertainty_coverage,
        )
        if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in rates):
            raise ValueError("uncertainty coverage metric is invalid")
        if self.probability_count == 0 and (
            self.brier_score is not None or self.expected_calibration_error is not None
        ):
            raise ValueError("unavailable probabilities cannot produce calibration scores")
        if self.probability_count > 0 and (self.brier_score is None or self.expected_calibration_error is None):
            raise ValueError("available probabilities require calibration scores")
        bucket_keys = tuple((item.dimension, item.bucket_index) for item in self.uncertainty_buckets)
        if len(bucket_keys) != len(set(bucket_keys)):
            raise ValueError("uncertainty buckets must be unique")


@dataclass(frozen=True)
class DeepSeekAblationDecision:
    trade_date: date
    code: str
    arm: DeepSeekAblationArm
    score: float
    veto: bool
    deepseek_applied: bool
    selected_rank: int | None

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit() or self.arm not in _ARMS:
            raise ValueError("DeepSeek ablation decision identity is invalid")
        if not math.isfinite(self.score) or not 0.0 <= self.score <= 100.0:
            raise ValueError("DeepSeek ablation decision score is invalid")
        if self.selected_rank is not None and not 1 <= self.selected_rank <= _TOP_K:
            raise ValueError("DeepSeek ablation selected rank is invalid")


@dataclass(frozen=True)
class DeepSeekAblationMetrics:
    arm: DeepSeekAblationArm
    evaluated_count: int
    selected_count: int
    deepseek_applied_count: int
    local_fallback_count: int
    mean_selected_net_excess_return: float | None
    selected_severe_loss_rate: float | None
    opportunity_cost: float

    def __post_init__(self) -> None:
        if self.arm not in _ARMS or self.evaluated_count < 1:
            raise ValueError("DeepSeek ablation metric identity is invalid")
        if not 0 <= self.selected_count <= self.evaluated_count:
            raise ValueError("DeepSeek ablation selected count is invalid")
        if not 0 <= self.deepseek_applied_count <= self.evaluated_count:
            raise ValueError("DeepSeek ablation applied count is invalid")
        if not 0 <= self.local_fallback_count <= self.evaluated_count:
            raise ValueError("DeepSeek ablation fallback count is invalid")
        if self.mean_selected_net_excess_return is not None and not math.isfinite(self.mean_selected_net_excess_return):
            raise ValueError("DeepSeek ablation selected return is invalid")
        if self.selected_severe_loss_rate is not None and (
            not math.isfinite(self.selected_severe_loss_rate) or not 0.0 <= self.selected_severe_loss_rate <= 1.0
        ):
            raise ValueError("DeepSeek ablation severe-loss rate is invalid")
        if not math.isfinite(self.opportunity_cost) or self.opportunity_cost < 0.0:
            raise ValueError("DeepSeek ablation opportunity cost is invalid")


@dataclass(frozen=True)
class RiskCostUncertaintyReport:
    status: RiskCostUncertaintyStatus
    parent_report_hash: str
    model_artifact_hash: str | None
    evidence_hash: str | None
    sample_count: int
    calibration: UncertaintyCalibrationMetrics | None
    ablation: tuple[DeepSeekAblationMetrics, ...]
    decisions: tuple[DeepSeekAblationDecision, ...]
    failure_reasons: tuple[str, ...]
    production_authority: bool = False
    terminal_holdout_opened: bool = False
    automatic_model_update: bool = False
    schema_version: str = "risk_cost_uncertainty_deepseek_report"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        reasons = tuple(sorted(set(self.failure_reasons)))
        _validate_report_identity(self)
        if self.status == "evaluated":
            _validate_evaluated_report(self, reasons)
        else:
            _validate_blocked_report(self, reasons)
        if self.production_authority or self.terminal_holdout_opened or self.automatic_model_update:
            raise ValueError("risk-cost research cannot authorize production or open terminal holdout")
        if any(_REASON_ID.fullmatch(item) is None for item in reasons):
            raise ValueError("risk-cost report failure reasons are invalid")
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "content_hash", _content_hash(self))


def _validate_report_identity(report: RiskCostUncertaintyReport) -> None:
    if report.status not in {"evaluated", "historical_data_insufficient", "historical_rejected"}:
        raise ValueError("risk-cost report status is invalid")
    if report.schema_version != "risk_cost_uncertainty_deepseek_report":
        raise ValueError("risk-cost report schema identity is invalid")
    if _SHA256.fullmatch(report.parent_report_hash) is None:
        raise ValueError("risk-cost report parent hash is invalid")
    if report.model_artifact_hash is not None and _SHA256.fullmatch(report.model_artifact_hash) is None:
        raise ValueError("risk-cost report model hash is invalid")
    if report.evidence_hash is not None and _SHA256.fullmatch(report.evidence_hash) is None:
        raise ValueError("risk-cost report evidence hash is invalid")


def _validate_evaluated_report(
    report: RiskCostUncertaintyReport,
    reasons: tuple[str, ...],
) -> None:
    if report.sample_count < 1 or report.calibration is None:
        raise ValueError("evaluated risk-cost report is incomplete")
    if report.calibration.sample_count != report.sample_count:
        raise ValueError("evaluated risk-cost calibration count is inconsistent")
    if tuple(item.arm for item in report.ablation) != _ARMS:
        raise ValueError("evaluated risk-cost report is incomplete")
    if reasons or report.model_artifact_hash is None or report.evidence_hash is None:
        raise ValueError("evaluated risk-cost report identity is inconsistent")
    if len(report.decisions) != report.sample_count * len(_ARMS):
        raise ValueError("evaluated risk-cost report identity is inconsistent")
    decision_keys = tuple((item.trade_date, item.code, item.arm) for item in report.decisions)
    if len(decision_keys) != len(set(decision_keys)):
        raise ValueError("evaluated risk-cost report decisions must be unique")
    _validate_ablation_decisions(report)


def _validate_ablation_decisions(report: RiskCostUncertaintyReport) -> None:
    populations: list[frozenset[tuple[date, str]]] = []
    for metrics in report.ablation:
        decisions = tuple(item for item in report.decisions if item.arm == metrics.arm)
        populations.append(frozenset((item.trade_date, item.code) for item in decisions))
        if metrics.evaluated_count != report.sample_count or len(decisions) != report.sample_count:
            raise ValueError("DeepSeek ablation population is inconsistent")
        if metrics.selected_count != sum(item.selected_rank is not None for item in decisions):
            raise ValueError("DeepSeek ablation selected count is inconsistent")
        if metrics.deepseek_applied_count != sum(item.deepseek_applied for item in decisions):
            raise ValueError("DeepSeek ablation applied count is inconsistent")
        expected_fallbacks = 0 if metrics.arm == "local_only" else report.sample_count - metrics.deepseek_applied_count
        if metrics.local_fallback_count != expected_fallbacks:
            raise ValueError("DeepSeek ablation fallback count is inconsistent")
        _validate_selected_ranks(decisions)
    if any(population != populations[0] for population in populations[1:]):
        raise ValueError("DeepSeek ablation arms must share one population")


def _validate_selected_ranks(decisions: tuple[DeepSeekAblationDecision, ...]) -> None:
    by_day: dict[date, list[int]] = defaultdict(list)
    for decision in decisions:
        if decision.selected_rank is not None:
            by_day[decision.trade_date].append(decision.selected_rank)
    for ranks in by_day.values():
        if sorted(ranks) != list(range(1, len(ranks) + 1)):
            raise ValueError("DeepSeek ablation ranks must be unique and contiguous")


def _validate_blocked_report(
    report: RiskCostUncertaintyReport,
    reasons: tuple[str, ...],
) -> None:
    if report.sample_count or report.calibration is not None or report.ablation or report.decisions or not reasons:
        raise ValueError("blocked risk-cost report must contain only bounded failure evidence")
    if report.evidence_hash is not None:
        raise ValueError("blocked risk-cost report cannot claim evaluated evidence")


def evaluate_risk_cost_uncertainty(
    parent_report_hash: str,
    model_artifact_hash: str,
    samples: tuple[RiskCostUncertaintySample, ...],
) -> RiskCostUncertaintyReport:
    if _SHA256.fullmatch(parent_report_hash) is None or _SHA256.fullmatch(model_artifact_hash) is None:
        raise ValueError("risk-cost parent identities must be SHA-256")
    ordered = tuple(sorted(samples, key=lambda item: (item.trade_date, item.code)))
    keys = tuple((item.trade_date, item.code) for item in ordered)
    if not ordered or len(keys) != len(set(keys)):
        raise ValueError("risk-cost samples must be non-empty and unique")
    decisions = _decisions(ordered)
    return RiskCostUncertaintyReport(
        "evaluated",
        parent_report_hash,
        model_artifact_hash,
        _sample_evidence_hash(ordered),
        len(ordered),
        _calibration(ordered),
        _ablation(ordered, decisions),
        decisions,
        (),
    )


def insufficient_risk_cost_uncertainty_report(
    parent_report_hash: str,
    model_artifact_hash: str | None,
    reasons: tuple[str, ...],
    *,
    rejected: bool = False,
) -> RiskCostUncertaintyReport:
    return RiskCostUncertaintyReport(
        "historical_rejected" if rejected else "historical_data_insufficient",
        parent_report_hash,
        model_artifact_hash,
        None,
        0,
        None,
        (),
        (),
        reasons,
    )


def _decisions(samples: tuple[RiskCostUncertaintySample, ...]) -> tuple[DeepSeekAblationDecision, ...]:
    drafts: list[DeepSeekAblationDecision] = []
    by_day: dict[tuple[date, DeepSeekAblationArm], list[tuple[RiskCostUncertaintySample, float, bool, bool]]] = (
        defaultdict(list)
    )
    for sample in samples:
        utility = build_selection_utility(sample.alpha, sample.risk, sample.cost)
        for arm in _ARMS:
            score, veto, applied = _arm_result(sample, utility, arm)
            by_day[(sample.trade_date, arm)].append((sample, score, veto, applied))
    for (trade_date, arm), rows in sorted(by_day.items()):
        selected = _select(rows)
        ranks = {item.code: rank for rank, item in enumerate(selected, start=1)}
        drafts.extend(
            DeepSeekAblationDecision(trade_date, sample.code, arm, score, veto, applied, ranks.get(sample.code))
            for sample, score, veto, applied in sorted(rows, key=lambda item: item[0].code)
        )
    return tuple(sorted(drafts, key=lambda item: (item.trade_date, item.code, _ARMS.index(item.arm))))


def _arm_result(
    sample: RiskCostUncertaintySample,
    utility: SelectionUtility,
    arm: DeepSeekAblationArm,
) -> tuple[float, bool, bool]:
    review = sample.deepseek
    applied = review.outcome == "applied"
    if arm == "local_only":
        return utility.local_score, sample.risk.veto, False
    veto = sample.risk.veto or (applied and review.structured_veto)
    if arm == "structured_facts_veto" or not applied:
        return utility.local_score, veto, applied
    assert review.score is not None
    local_weight = 0.68
    deepseek_weight = 0.32
    score = round_score(
        clamp(utility.local_score * local_weight + review.score * deepseek_weight - review.structured_risk_penalty)
    )
    return score, veto, True


def _select(
    rows: list[tuple[RiskCostUncertaintySample, float, bool, bool]],
) -> tuple[RiskCostUncertaintySample, ...]:
    eligible = [
        (sample, score)
        for sample, score, veto, _applied in rows
        if not veto and build_selection_utility(sample.alpha, sample.risk, sample.cost).eligible
    ]
    eligible.sort(
        key=lambda item: (
            -item[1],
            -build_selection_utility(item[0].alpha, item[0].risk, item[0].cost).expected_net_excess_return,
            item[0].risk.uncertainty.severe_loss_probability
            if item[0].risk.uncertainty.severe_loss_probability is not None
            else 1.0,
            item[0].code,
        )
    )
    selected: list[RiskCostUncertaintySample] = []
    boards: dict[str, int] = {}
    industries: dict[str, int] = {}
    for sample, _score in eligible:
        if boards.get(sample.board, 0) >= _MAX_PER_BOARD or industries.get(sample.industry, 0) >= _MAX_PER_INDUSTRY:
            continue
        selected.append(sample)
        boards[sample.board] = boards.get(sample.board, 0) + 1
        industries[sample.industry] = industries.get(sample.industry, 0) + 1
        if len(selected) == _TOP_K:
            break
    return tuple(selected)


def _calibration(samples: tuple[RiskCostUncertaintySample, ...]) -> UncertaintyCalibrationMetrics:
    probabilities = tuple(
        (sample.risk.uncertainty.severe_loss_probability, float(sample.actual_severe_loss))
        for sample in samples
        if sample.risk.uncertainty.severe_loss_probability is not None
    )
    probability_values = tuple((float(probability), target) for probability, target in probabilities)
    intervals = tuple(sample for sample in samples if sample.risk.uncertainty.prediction_interval is not None)
    interval_hits = tuple(
        sample.risk.uncertainty.prediction_interval.lower_excess_return
        <= sample.actual_alpha_return
        <= sample.risk.uncertainty.prediction_interval.upper_excess_return
        for sample in intervals
        if sample.risk.uncertainty.prediction_interval is not None
    )
    total = len(samples)
    return UncertaintyCalibrationMetrics(
        total,
        len(probability_values),
        fmean((probability - target) ** 2 for probability, target in probability_values)
        if probability_values
        else None,
        _ece(probability_values) if probability_values else None,
        len(probability_values) / total,
        fmean(interval_hits) if interval_hits else None,
        len(intervals) / total,
        _coverage(tuple(item.risk.uncertainty.model_disagreement for item in samples)),
        _coverage(tuple(item.risk.uncertainty.training_window_disagreement for item in samples)),
        _coverage(tuple(item.risk.uncertainty.ood_distance for item in samples)),
        _coverage(tuple(item.risk.uncertainty.missing_uncertainty for item in samples)),
        _uncertainty_buckets(samples),
    )


def _ece(values: tuple[tuple[float, float], ...]) -> float:
    total = len(values)
    error = 0.0
    for bin_index in range(10):
        lower = bin_index / 10.0
        upper = (bin_index + 1) / 10.0
        members = tuple(item for item in values if lower <= item[0] < upper or bin_index == 9 and item[0] == 1.0)
        if members:
            error += len(members) / total * abs(fmean(item[0] for item in members) - fmean(item[1] for item in members))
    return error


def _coverage(values: tuple[float | None, ...]) -> float:
    return sum(value is not None for value in values) / len(values)


def _uncertainty_buckets(
    samples: tuple[RiskCostUncertaintySample, ...],
) -> tuple[UncertaintyBucketMetrics, ...]:
    dimensions: tuple[tuple[UncertaintyDimension, tuple[float | None, ...]], ...] = (
        ("model_disagreement", tuple(item.risk.uncertainty.model_disagreement for item in samples)),
        (
            "training_window_disagreement",
            tuple(item.risk.uncertainty.training_window_disagreement for item in samples),
        ),
        ("ood_distance", tuple(item.risk.uncertainty.ood_distance for item in samples)),
        ("missing_uncertainty", tuple(item.risk.uncertainty.missing_uncertainty for item in samples)),
    )
    result: list[UncertaintyBucketMetrics] = []
    for dimension, values in dimensions:
        observed = tuple(
            sorted(
                ((float(value), sample) for sample, value in zip(samples, values, strict=True) if value is not None),
                key=lambda item: (item[0], item[1].trade_date, item[1].code),
            )
        )
        if observed:
            result.extend(_bucket_dimension(dimension, observed))
    return tuple(result)


def _bucket_dimension(
    dimension: UncertaintyDimension,
    observed: tuple[tuple[float, RiskCostUncertaintySample], ...],
) -> tuple[UncertaintyBucketMetrics, ...]:
    grouped: dict[int, list[tuple[float, RiskCostUncertaintySample]]] = defaultdict(list)
    distinct_values = tuple(sorted({value for value, _sample in observed}))
    bucket_by_value = {
        value: min(3, index * 3 // len(distinct_values) + 1) for index, value in enumerate(distinct_values)
    }
    for item in observed:
        grouped[bucket_by_value[item[0]]].append(item)
    return tuple(
        UncertaintyBucketMetrics(
            dimension,
            bucket_index,
            len(rows),
            rows[0][0],
            rows[-1][0],
            fmean(item.actual_net_excess_return for _value, item in rows),
            fmean(float(item.actual_severe_loss) for _value, item in rows),
        )
        for bucket_index, rows in sorted(grouped.items())
    )


def _ablation(
    samples: tuple[RiskCostUncertaintySample, ...],
    decisions: tuple[DeepSeekAblationDecision, ...],
) -> tuple[DeepSeekAblationMetrics, ...]:
    sample_by_key = {(item.trade_date, item.code): item for item in samples}
    oracle_total = _oracle_total(samples)
    result: list[DeepSeekAblationMetrics] = []
    for arm in _ARMS:
        arm_decisions = tuple(item for item in decisions if item.arm == arm)
        selected = tuple(
            sample_by_key[(item.trade_date, item.code)] for item in arm_decisions if item.selected_rank is not None
        )
        realized = tuple(item.actual_net_excess_return for item in selected)
        result.append(
            DeepSeekAblationMetrics(
                arm,
                len(arm_decisions),
                len(selected),
                sum(item.deepseek_applied for item in arm_decisions),
                sum(sample_by_key[(item.trade_date, item.code)].deepseek.outcome != "applied" for item in arm_decisions)
                if arm != "local_only"
                else 0,
                fmean(realized) if realized else None,
                fmean(float(item.actual_severe_loss) for item in selected) if selected else None,
                max(0.0, oracle_total - sum(realized)),
            )
        )
    return tuple(result)


def _oracle_total(samples: tuple[RiskCostUncertaintySample, ...]) -> float:
    by_day: dict[date, list[RiskCostUncertaintySample]] = defaultdict(list)
    for sample in samples:
        by_day[sample.trade_date].append(sample)
    total = 0.0
    for rows in by_day.values():
        ranked = sorted(
            (sample for sample in rows if build_selection_utility(sample.alpha, sample.risk, sample.cost).eligible),
            key=lambda item: (-item.actual_net_excess_return, item.code),
        )
        boards: dict[str, int] = {}
        industries: dict[str, int] = {}
        selected = 0
        for sample in ranked:
            if sample.actual_net_excess_return <= 0.0:
                break
            if boards.get(sample.board, 0) >= _MAX_PER_BOARD:
                continue
            if industries.get(sample.industry, 0) >= _MAX_PER_INDUSTRY:
                continue
            total += sample.actual_net_excess_return
            selected += 1
            boards[sample.board] = boards.get(sample.board, 0) + 1
            industries[sample.industry] = industries.get(sample.industry, 0) + 1
            if selected == _TOP_K:
                break
    return total


def _content_hash(report: RiskCostUncertaintyReport) -> str:
    payload = {
        "schema_version": report.schema_version,
        "status": report.status,
        "parent_report_hash": report.parent_report_hash,
        "model_artifact_hash": report.model_artifact_hash,
        "evidence_hash": report.evidence_hash,
        "sample_count": report.sample_count,
        "calibration": None if report.calibration is None else _calibration_payload(report.calibration),
        "ablation": tuple(_ablation_payload(item) for item in report.ablation),
        "decisions": tuple(_decision_payload(item) for item in report.decisions),
        "failure_reasons": report.failure_reasons,
        "production_authority": report.production_authority,
        "terminal_holdout_opened": report.terminal_holdout_opened,
        "automatic_model_update": report.automatic_model_update,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _sample_evidence_hash(samples: tuple[RiskCostUncertaintySample, ...]) -> str:
    payload = tuple(
        {
            "trade_date": sample.trade_date.isoformat(),
            "board": sample.board,
            "industry": sample.industry,
            "alpha": {
                "code": sample.alpha.code,
                "profile_id": sample.alpha.profile_id,
                "model_id": sample.alpha.model_id,
                "model_hash": sample.alpha.model_hash,
                "feature_vector_hash": sample.alpha.feature_vector_hash,
                "signal_score": sample.alpha.signal_score,
                "predicted_excess_return": sample.alpha.predicted_excess_return,
            },
            "risk": {
                "code": sample.risk.code,
                "penalty_points": sample.risk.penalty_points,
                "veto": sample.risk.veto,
                "structured_fact_ids": sample.risk.structured_fact_ids,
                "uncertainty": _uncertainty_evidence_payload(sample.risk),
            },
            "cost": {
                "code": sample.cost.code,
                "estimated_round_trip_return": sample.cost.estimated_round_trip_return,
                "capacity_amount": sample.cost.capacity_amount,
                "participation_rate": sample.cost.participation_rate,
                "scenarios": tuple((item.scenario_id, item.round_trip_return) for item in sample.cost.scenarios),
            },
            "deepseek": {
                "outcome": sample.deepseek.outcome,
                "score": sample.deepseek.score,
                "structured_risk_penalty": sample.deepseek.structured_risk_penalty,
                "structured_veto": sample.deepseek.structured_veto,
                "structured_fact_ids": sample.deepseek.structured_fact_ids,
            },
            "actual_alpha_return": sample.actual_alpha_return,
            "actual_net_excess_return": sample.actual_net_excess_return,
            "actual_severe_loss": sample.actual_severe_loss,
        }
        for sample in samples
    )
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _uncertainty_evidence_payload(risk: RiskDecision) -> dict[str, float | tuple[float, float] | None]:
    interval = risk.uncertainty.prediction_interval
    return {
        "severe_loss_probability": risk.uncertainty.severe_loss_probability,
        "prediction_interval": (
            None if interval is None else (interval.lower_excess_return, interval.upper_excess_return)
        ),
        "model_disagreement": risk.uncertainty.model_disagreement,
        "training_window_disagreement": risk.uncertainty.training_window_disagreement,
        "ood_distance": risk.uncertainty.ood_distance,
        "missing_uncertainty": risk.uncertainty.missing_uncertainty,
    }


def _calibration_payload(
    metrics: UncertaintyCalibrationMetrics,
) -> dict[
    str,
    int | float | None | tuple[dict[str, str | int | float], ...],
]:
    return {
        "sample_count": metrics.sample_count,
        "probability_count": metrics.probability_count,
        "brier_score": metrics.brier_score,
        "expected_calibration_error": metrics.expected_calibration_error,
        "probability_coverage": metrics.probability_coverage,
        "prediction_interval_coverage": metrics.prediction_interval_coverage,
        "prediction_interval_availability": metrics.prediction_interval_availability,
        "model_disagreement_coverage": metrics.model_disagreement_coverage,
        "training_window_disagreement_coverage": metrics.training_window_disagreement_coverage,
        "ood_coverage": metrics.ood_coverage,
        "missing_uncertainty_coverage": metrics.missing_uncertainty_coverage,
        "uncertainty_buckets": tuple(_uncertainty_bucket_payload(item) for item in metrics.uncertainty_buckets),
    }


def _uncertainty_bucket_payload(
    metrics: UncertaintyBucketMetrics,
) -> dict[str, str | int | float]:
    return {
        "dimension": metrics.dimension,
        "bucket_index": metrics.bucket_index,
        "sample_count": metrics.sample_count,
        "lower_value": metrics.lower_value,
        "upper_value": metrics.upper_value,
        "mean_net_excess_return": metrics.mean_net_excess_return,
        "severe_loss_rate": metrics.severe_loss_rate,
    }


def _ablation_payload(metrics: DeepSeekAblationMetrics) -> dict[str, str | int | float | None]:
    return {
        "arm": metrics.arm,
        "evaluated_count": metrics.evaluated_count,
        "selected_count": metrics.selected_count,
        "deepseek_applied_count": metrics.deepseek_applied_count,
        "local_fallback_count": metrics.local_fallback_count,
        "mean_selected_net_excess_return": metrics.mean_selected_net_excess_return,
        "selected_severe_loss_rate": metrics.selected_severe_loss_rate,
        "opportunity_cost": metrics.opportunity_cost,
    }


def _decision_payload(decision: DeepSeekAblationDecision) -> dict[str, str | int | float | bool | None]:
    return {
        "trade_date": decision.trade_date.isoformat(),
        "code": decision.code,
        "arm": decision.arm,
        "score": decision.score,
        "veto": decision.veto,
        "deepseek_applied": decision.deepseek_applied,
        "selected_rank": decision.selected_rank,
    }


__all__ = [
    "DeepSeekAblationDecision",
    "DeepSeekAblationMetrics",
    "DeepSeekResearchReview",
    "RiskCostUncertaintyReport",
    "RiskCostUncertaintySample",
    "SelectionUtility",
    "UncertaintyBucketMetrics",
    "UncertaintyCalibrationMetrics",
    "build_selection_utility",
    "evaluate_risk_cost_uncertainty",
    "insufficient_risk_cost_uncertainty_report",
]
