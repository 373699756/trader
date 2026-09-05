"""Point-in-time filter funnel ablation contracts.

The ablation is deliberately small and deterministic.  Permanent issuer facts and
safety vetoes are controls; only the registered secondary rules may be removed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

FilterRuleId = Literal[
    "evidence",
    "candidate_missing",
    "candidate_reliability",
    "candidate_score",
    "candidate_cap",
]
FilterRuleClassification = Literal["retain", "observe_candidate", "interaction_diagnostic"]
ABLATION_RULES: tuple[FilterRuleId, ...] = (
    "evidence",
    "candidate_missing",
    "candidate_reliability",
    "candidate_score",
    "candidate_cap",
)

_CODE = re.compile(r"^\d{6}$")


@dataclass(frozen=True, order=True)
class FilterScoreComponent:
    name: str
    value: float
    weight: float

    def __post_init__(self) -> None:
        if not self.name or any(not math.isfinite(value) for value in (self.value, self.weight)):
            raise ValueError("filter score component is invalid")
        if not 0.0 <= self.value <= 100.0 or not 0.0 < self.weight <= 1.0:
            raise ValueError("filter score component value or weight is out of range")


@dataclass(frozen=True, order=True)
class FilterAblationRow:
    trade_date: date
    code: str
    board: str
    industry: str
    permanent_eligible: bool
    safety_veto: bool
    evidence_complete: bool
    candidate_present: bool
    candidate_reliable: bool
    candidate_score: float | None
    candidate_rank: int | None
    actual_net_excess_20bp: float
    actual_net_excess_50bp: float
    severe_loss: bool
    io_requests: int = 0
    scoring_rows: int = 0
    deepseek_requests: int = 0
    latency_ms: float = 0.0
    local_action_passed: bool = True
    topk_concentration_passed: bool = True
    predicted_severe_loss_risk: float | None = None
    mae_atr20: float = 0.0
    capacity: float = 1.0
    score_components: tuple[FilterScoreComponent, ...] = ()

    def __post_init__(self) -> None:
        _validate_filter_ablation_identity(self)
        _validate_filter_ablation_candidate(self)
        _validate_filter_score_components(self)
        if min(self.io_requests, self.scoring_rows, self.deepseek_requests) < 0:
            raise ValueError("filter ablation resource counts cannot be negative")

    @property
    def eligible_profit(self) -> bool:
        return self.permanent_eligible and not self.safety_veto and self.actual_net_excess_20bp > 0.0

    @property
    def eligible_profit_50bp(self) -> bool:
        return self.permanent_eligible and not self.safety_veto and self.actual_net_excess_50bp > 0.0

    def blocked_by(self, rule: FilterRuleId | None = None) -> tuple[str, ...]:
        checks: tuple[tuple[str, bool], ...] = (
            ("evidence", not self.evidence_complete),
            ("candidate_missing", not self.candidate_present),
            ("candidate_reliability", self.candidate_present and not self.candidate_reliable),
            ("candidate_score", self.candidate_score is not None and self.candidate_score < 50.0),
            ("candidate_cap", self.candidate_present and self.candidate_rank is not None and self.candidate_rank > 120),
            ("local_action", not self.local_action_passed),
            ("topk_concentration", not self.topk_concentration_passed),
        )
        values = tuple(name for name, blocked in checks if blocked)
        return values if rule is None else tuple(name for name in values if name == rule)

    @property
    def matched_rules(self) -> tuple[str, ...]:
        """All secondary rules hit by this row, independent of evaluation order."""
        return self.blocked_by()

    @property
    def first_blocking_rule(self) -> str | None:
        return self.matched_rules[0] if self.matched_rules else None

    @property
    def exclusive_blocking_rule(self) -> str | None:
        return self.matched_rules[0] if len(self.matched_rules) == 1 else None

    @property
    def enters_next_layer(self) -> bool:
        return self.passes()

    def passes(self, disabled: frozenset[FilterRuleId] = frozenset()) -> bool:
        if not self.permanent_eligible or self.safety_veto:
            return False
        return (
            not any(rule not in disabled for rule in self.blocked_by() if rule in ABLATION_RULES)
            and self.local_action_passed
            and self.topk_concentration_passed
        )


def _validate_filter_ablation_identity(row: FilterAblationRow) -> None:
    if _CODE.fullmatch(row.code) is None or not row.board or not row.industry:
        raise ValueError("filter ablation row identity is invalid")
    values = (
        row.actual_net_excess_20bp,
        row.actual_net_excess_50bp,
        row.latency_ms,
        row.mae_atr20,
        row.capacity,
    )
    if any(not math.isfinite(value) for value in values) or row.latency_ms < 0:
        raise ValueError("filter ablation numeric values must be finite")


def _validate_filter_ablation_candidate(row: FilterAblationRow) -> None:
    if row.candidate_score is not None and (
        not math.isfinite(row.candidate_score) or not 0.0 <= row.candidate_score <= 100.0
    ):
        raise ValueError("filter ablation candidate score is invalid")
    if row.candidate_rank is not None and row.candidate_rank < 1:
        raise ValueError("filter ablation candidate rank is invalid")
    if row.candidate_present != (row.candidate_score is not None and row.candidate_rank is not None):
        raise ValueError("present candidate requires score and rank; missing candidate requires neither")
    if row.predicted_severe_loss_risk is not None and (
        not math.isfinite(row.predicted_severe_loss_risk) or not 0.0 <= row.predicted_severe_loss_risk <= 1.0
    ):
        raise ValueError("filter ablation predicted severe-loss risk must be in [0, 1]")
    if row.capacity < 0.0:
        raise ValueError("filter ablation capacity cannot be negative")


def _validate_filter_score_components(row: FilterAblationRow) -> None:
    if not row.score_components:
        return
    if len({item.name for item in row.score_components}) != len(row.score_components):
        raise ValueError("filter score component names must be unique")
    if not math.isclose(
        math.fsum(item.weight for item in row.score_components),
        1.0,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise ValueError("filter score component weights must sum to one")
    component_score = math.fsum(item.value * item.weight for item in row.score_components)
    if row.candidate_score is None or not math.isclose(
        component_score,
        row.candidate_score,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise ValueError("filter score components must bind the candidate score")


@dataclass(frozen=True, order=True)
class FilterDistributionCount:
    key: str
    count: int

    def __post_init__(self) -> None:
        if not self.key or self.count < 0:
            raise ValueError("filter distribution count is invalid")


@dataclass(frozen=True)
class FilterVariantMetrics:
    retained_count: int
    profitable_executable_recall_20bp: float | None
    profitable_executable_recall_50bp: float | None
    severe_loss_interception: float | None
    scorable_coverage: float
    candidate_stability: float | None
    turnover: float
    capacity: float
    board_distribution: tuple[FilterDistributionCount, ...]
    industry_distribution: tuple[FilterDistributionCount, ...]

    def __post_init__(self) -> None:
        if self.retained_count < 0:
            raise ValueError("filter variant retained count cannot be negative")
        rates = (
            self.profitable_executable_recall_20bp,
            self.profitable_executable_recall_50bp,
            self.severe_loss_interception,
            self.scorable_coverage,
            self.candidate_stability,
            self.turnover,
        )
        if any(value is not None and (not math.isfinite(value) or not 0.0 <= value <= 1.0) for value in rates):
            raise ValueError("filter variant rates must be in [0, 1]")
        if not math.isfinite(self.capacity) or self.capacity < 0.0:
            raise ValueError("filter variant capacity must be finite and non-negative")


@dataclass(frozen=True)
class FilterRuleContribution:
    rule: str
    baseline_count: int
    retained_count: int
    profitable_executable_recall: float | None
    severe_loss_interception: float | None
    io_saved: int
    scoring_rows_saved: int
    deepseek_requests_saved: int
    p95_latency_ms: float | None
    variant_metrics: FilterVariantMetrics | None = None
    first_blocked_count: int = 0
    exclusive_blocked_count: int = 0
    recall_delta_20bp: float | None = None
    recall_delta_50bp: float | None = None
    classification: FilterRuleClassification = "retain"

    def __post_init__(self) -> None:
        if self.rule not in ABLATION_RULES and not self.rule.startswith("interaction_") and self.rule != "control":
            raise ValueError("filter ablation rule is not registered")
        if (
            min(
                self.baseline_count,
                self.retained_count,
                self.io_saved,
                self.scoring_rows_saved,
                self.deepseek_requests_saved,
                self.first_blocked_count,
                self.exclusive_blocked_count,
            )
            < 0
        ):
            raise ValueError("filter ablation counts cannot be negative")
        for value in (self.profitable_executable_recall, self.severe_loss_interception, self.p95_latency_ms):
            if value is not None and (not math.isfinite(value) or value < 0.0):
                raise ValueError("filter ablation metrics must be finite")
        for value in (self.profitable_executable_recall, self.severe_loss_interception):
            if value is not None and value > 1.0:
                raise ValueError("filter ablation rates must be in [0, 1]")
        if any(
            value is not None and (not math.isfinite(value) or not -1.0 <= value <= 1.0)
            for value in (self.recall_delta_20bp, self.recall_delta_50bp)
        ):
            raise ValueError("filter ablation recall deltas must be in [-1, 1]")
        expected_classifications = (
            {"interaction_diagnostic"}
            if self.rule.startswith("interaction_")
            else {
                "retain",
                "observe_candidate",
            }
        )
        if self.classification not in expected_classifications:
            raise ValueError("filter ablation rule classification is inconsistent")


@dataclass(frozen=True)
class FilterRecallAblationReport:
    strategy: str
    development_dates: tuple[date, ...]
    rows: int
    profitable_denominator: int
    baseline_recall: float | None
    baseline_severe_loss_interception: float | None
    contributions: tuple[FilterRuleContribution, ...]
    interactions: tuple[FilterRuleContribution, ...]
    recommendations: tuple[str, ...]
    terminal_status: Literal["complete", "historical_data_insufficient"]
    production_authority: bool = False
    schema_version: str = "historical_filter_recall_ablation_report"
    baseline_recall_50bp: float | None = None
    baseline_metrics: FilterVariantMetrics | None = None
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if not self.strategy or not self.development_dates or self.rows < 0:
            raise ValueError("filter ablation report requires strategy, dates, and rows")
        if self.terminal_status not in {"complete", "historical_data_insufficient"}:
            raise ValueError("filter ablation terminal status is invalid")
        if self.profitable_denominator < 0 or self.profitable_denominator > self.rows:
            raise ValueError("filter ablation profitable denominator is invalid")
        if self.production_authority or self.schema_version != "historical_filter_recall_ablation_report":
            raise ValueError("filter ablation report cannot authorize production")
        object.__setattr__(self, "development_dates", tuple(sorted(set(self.development_dates))))
        object.__setattr__(self, "recommendations", tuple(sorted(set(self.recommendations))))
        object.__setattr__(self, "content_hash", _canonical_hash(self))


def run_filter_recall_ablation(
    rows: tuple[FilterAblationRow, ...],
    *,
    strategy: str,
    development_dates: tuple[date, ...],
    interaction_rules: tuple[tuple[FilterRuleId, FilterRuleId], ...] = (("evidence", "candidate_reliability"),),
) -> FilterRecallAblationReport:
    """Replay controls and leave-one-rule-out variants on development dates only."""

    if not rows:
        return FilterRecallAblationReport(
            strategy,
            tuple(development_dates),
            0,
            0,
            None,
            None,
            (),
            (),
            (),
            "historical_data_insufficient",
        )
    dates = frozenset(development_dates)
    selected = tuple(
        sorted((row for row in rows if row.trade_date in dates), key=lambda row: (row.trade_date, row.code))
    )
    if not selected:
        return FilterRecallAblationReport(
            strategy, tuple(development_dates), 0, 0, None, None, (), (), (), "historical_data_insufficient"
        )
    denominator = sum(row.eligible_profit for row in selected)
    denominator_50bp = sum(row.eligible_profit_50bp for row in selected)
    baseline = tuple(row for row in selected if row.passes())
    baseline_metrics = _variant_metrics(baseline, selected, tuple(development_dates))
    baseline_recall = _recall(baseline, denominator, cost="20bp")
    baseline_recall_50bp = _recall(baseline, denominator_50bp, cost="50bp")
    baseline_interception = baseline_metrics.severe_loss_interception
    contribution_context = _ContributionContext(baseline, selected, tuple(development_dates))
    contributions: list[FilterRuleContribution] = []
    for rule in ABLATION_RULES:
        retained = tuple(row for row in selected if row.passes(frozenset({rule})))
        contributions.append(_contribution(rule, retained, contribution_context))
    interactions: list[FilterRuleContribution] = []
    for left, right in interaction_rules:
        if left not in ABLATION_RULES or right not in ABLATION_RULES or left == right:
            raise ValueError("filter ablation interaction is not preregistered")
        retained = tuple(row for row in selected if row.passes(frozenset({left, right})))
        interactions.append(
            _contribution(
                f"interaction_{left}_{right}",
                retained,
                contribution_context,
                allow_interaction=True,
            )
        )
    recommendations = tuple(item.rule for item in contributions if item.classification == "observe_candidate")
    return FilterRecallAblationReport(
        strategy=strategy,
        development_dates=tuple(development_dates),
        rows=len(selected),
        profitable_denominator=denominator,
        baseline_recall=baseline_recall,
        baseline_severe_loss_interception=baseline_interception,
        contributions=tuple(contributions),
        interactions=tuple(interactions),
        recommendations=tuple(recommendations),
        terminal_status="complete",
        baseline_recall_50bp=baseline_recall_50bp,
        baseline_metrics=baseline_metrics,
    )


@dataclass(frozen=True)
class _ContributionContext:
    baseline: tuple[FilterAblationRow, ...]
    population: tuple[FilterAblationRow, ...]
    dates: tuple[date, ...]


def _contribution(
    rule: str,
    retained: tuple[FilterAblationRow, ...],
    context: _ContributionContext,
    *,
    allow_interaction: bool = False,
) -> FilterRuleContribution:
    if not allow_interaction and rule not in ABLATION_RULES:
        raise ValueError("filter ablation rule is not registered")
    p95 = _percentile(tuple(row.latency_ms for row in retained), 0.95)
    metrics = _variant_metrics(retained, context.population, context.dates)
    baseline_metrics = _variant_metrics(context.baseline, context.population, context.dates)
    recall_delta_20bp = _difference(
        metrics.profitable_executable_recall_20bp,
        baseline_metrics.profitable_executable_recall_20bp,
    )
    recall_delta_50bp = _difference(
        metrics.profitable_executable_recall_50bp,
        baseline_metrics.profitable_executable_recall_50bp,
    )
    fixed_population = tuple(row for row in context.population if row.permanent_eligible and not row.safety_veto)
    severe_not_worse = baseline_metrics.severe_loss_interception is None or (
        metrics.severe_loss_interception is not None
        and metrics.severe_loss_interception >= baseline_metrics.severe_loss_interception
    )
    classification: FilterRuleClassification = (
        "interaction_diagnostic"
        if allow_interaction
        else (
            "observe_candidate"
            if recall_delta_20bp is not None
            and recall_delta_20bp > 0.0
            and recall_delta_50bp is not None
            and recall_delta_50bp > 0.0
            and severe_not_worse
            else "retain"
        )
    )
    return FilterRuleContribution(
        rule=rule,
        baseline_count=len(context.baseline),
        retained_count=len(retained),
        profitable_executable_recall=metrics.profitable_executable_recall_20bp,
        severe_loss_interception=metrics.severe_loss_interception,
        io_saved=max(0, sum(row.io_requests for row in retained) - sum(row.io_requests for row in context.baseline)),
        scoring_rows_saved=max(
            0, sum(row.scoring_rows for row in retained) - sum(row.scoring_rows for row in context.baseline)
        ),
        deepseek_requests_saved=max(
            0,
            sum(row.deepseek_requests for row in retained) - sum(row.deepseek_requests for row in context.baseline),
        ),
        p95_latency_ms=p95,
        variant_metrics=metrics,
        first_blocked_count=(
            0 if allow_interaction else sum(row.first_blocking_rule == rule for row in fixed_population)
        ),
        exclusive_blocked_count=(
            0 if allow_interaction else sum(row.exclusive_blocking_rule == rule for row in fixed_population)
        ),
        recall_delta_20bp=recall_delta_20bp,
        recall_delta_50bp=recall_delta_50bp,
        classification=classification,
    )


def _difference(value: float | None, baseline: float | None) -> float | None:
    return None if value is None or baseline is None else value - baseline


def _recall(
    rows: tuple[FilterAblationRow, ...],
    denominator: int,
    *,
    cost: Literal["20bp", "50bp"],
) -> float | None:
    if denominator == 0:
        return None
    numerator = sum(row.eligible_profit if cost == "20bp" else row.eligible_profit_50bp for row in rows)
    return numerator / denominator


def _variant_metrics(
    rows: tuple[FilterAblationRow, ...],
    population: tuple[FilterAblationRow, ...],
    dates: tuple[date, ...],
) -> FilterVariantMetrics:
    fixed_population = tuple(row for row in population if row.permanent_eligible and not row.safety_veto)
    severe_population = tuple(row for row in fixed_population if row.severe_loss)
    retained_severe = sum(row.severe_loss for row in rows)
    interception = None if not severe_population else 1.0 - retained_severe / len(severe_population)
    selected_by_date = {
        trade_date: frozenset(row.code for row in rows if row.trade_date == trade_date)
        for trade_date in sorted(set(dates))
    }
    adjacent = tuple(zip(selected_by_date.values(), tuple(selected_by_date.values())[1:], strict=False))
    stability_values = tuple(_jaccard(left, right) for left, right in adjacent)
    distributions = (_distribution(rows, "board"), _distribution(rows, "industry"))
    return FilterVariantMetrics(
        retained_count=len(rows),
        profitable_executable_recall_20bp=_recall(
            rows,
            sum(row.eligible_profit for row in fixed_population),
            cost="20bp",
        ),
        profitable_executable_recall_50bp=_recall(
            rows,
            sum(row.eligible_profit_50bp for row in fixed_population),
            cost="50bp",
        ),
        severe_loss_interception=interception,
        scorable_coverage=len(rows) / len(fixed_population) if fixed_population else 0.0,
        candidate_stability=math.fsum(stability_values) / len(stability_values) if stability_values else None,
        turnover=1.0 - math.fsum(stability_values) / len(stability_values) if stability_values else 0.0,
        capacity=min((row.capacity for row in rows), default=0.0),
        board_distribution=distributions[0],
        industry_distribution=distributions[1],
    )


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def _distribution(
    rows: tuple[FilterAblationRow, ...], field: Literal["board", "industry"]
) -> tuple[FilterDistributionCount, ...]:
    counts: dict[str, int] = {}
    for row in rows:
        key = getattr(row, field)
        counts[key] = counts.get(key, 0) + 1
    return tuple(FilterDistributionCount(key, count) for key, count in sorted(counts.items()))


def _percentile(values: tuple[float, ...], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(probability * len(ordered)) - 1))
    return ordered[index]


def _canonical_hash(value: object) -> str:
    def encode(item: object) -> object:
        if dataclasses.is_dataclass(item):
            return {field.name: encode(getattr(item, field.name)) for field in dataclasses.fields(item) if field.init}
        if isinstance(item, date):
            return item.isoformat()
        if isinstance(item, (tuple, list)):
            return [encode(child) for child in item]
        if isinstance(item, dict):
            return {str(key): encode(child) for key, child in item.items()}
        return item

    payload = json.dumps(encode(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


__all__ = [
    "ABLATION_RULES",
    "FilterAblationRow",
    "FilterDistributionCount",
    "FilterRuleContribution",
    "FilterRuleClassification",
    "FilterRecallAblationReport",
    "FilterScoreComponent",
    "FilterVariantMetrics",
    "run_filter_recall_ablation",
]
