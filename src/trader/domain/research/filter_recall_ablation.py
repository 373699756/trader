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
ABLATION_RULES: tuple[FilterRuleId, ...] = (
    "evidence",
    "candidate_missing",
    "candidate_reliability",
    "candidate_score",
    "candidate_cap",
)

_CODE = re.compile(r"^\d{6}$")


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

    def __post_init__(self) -> None:
        if _CODE.fullmatch(self.code) is None or not self.board or not self.industry:
            raise ValueError("filter ablation row identity is invalid")
        values = (self.actual_net_excess_20bp, self.actual_net_excess_50bp, self.latency_ms)
        if any(not math.isfinite(value) for value in values) or self.latency_ms < 0:
            raise ValueError("filter ablation numeric values must be finite")
        if self.candidate_score is not None and (
            not math.isfinite(self.candidate_score) or not 0.0 <= self.candidate_score <= 100.0
        ):
            raise ValueError("filter ablation candidate score is invalid")
        if self.candidate_rank is not None and self.candidate_rank < 1:
            raise ValueError("filter ablation candidate rank is invalid")
        if min(self.io_requests, self.scoring_rows, self.deepseek_requests) < 0:
            raise ValueError("filter ablation resource counts cannot be negative")

    @property
    def eligible_profit(self) -> bool:
        return self.permanent_eligible and not self.safety_veto and self.actual_net_excess_20bp > 0.0

    def blocked_by(self, rule: FilterRuleId | None = None) -> tuple[str, ...]:
        checks: tuple[tuple[str, bool], ...] = (
            ("evidence", not self.evidence_complete),
            ("candidate_missing", not self.candidate_present),
            ("candidate_reliability", self.candidate_present and not self.candidate_reliable),
            ("candidate_score", self.candidate_present and (self.candidate_score or 0.0) < 50.0),
            ("candidate_cap", self.candidate_present and self.candidate_rank is not None and self.candidate_rank > 120),
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
        return not any(rule not in disabled for rule in self.blocked_by())


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

    def __post_init__(self) -> None:
        if self.rule not in ABLATION_RULES and not self.rule.startswith("interaction_") and self.rule != "control":
            raise ValueError("filter ablation rule is not registered")
        if min(self.baseline_count, self.retained_count, self.io_saved, self.scoring_rows_saved, self.deepseek_requests_saved) < 0:
            raise ValueError("filter ablation counts cannot be negative")
        for value in (self.profitable_executable_recall, self.severe_loss_interception, self.p95_latency_ms):
            if value is not None and (not math.isfinite(value) or value < 0.0):
                raise ValueError("filter ablation metrics must be finite")
        for value in (self.profitable_executable_recall, self.severe_loss_interception):
            if value is not None and value > 1.0:
                raise ValueError("filter ablation rates must be in [0, 1]")


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
    schema_version: str = "historical_filter_recall_ablation_report_v1"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if not self.strategy or not self.development_dates or self.rows < 0:
            raise ValueError("filter ablation report requires strategy, dates, and rows")
        if self.profitable_denominator < 0 or self.profitable_denominator > self.rows:
            raise ValueError("filter ablation profitable denominator is invalid")
        if self.production_authority or self.schema_version != "historical_filter_recall_ablation_report_v1":
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
        raise ValueError("filter ablation requires rows")
    dates = frozenset(development_dates)
    selected = tuple(sorted((row for row in rows if row.trade_date in dates), key=lambda row: (row.trade_date, row.code)))
    if not selected:
        return FilterRecallAblationReport(strategy, tuple(development_dates), 0, 0, None, None, (), (), (), "historical_data_insufficient")
    denominator = sum(row.eligible_profit for row in selected)
    baseline = tuple(row for row in selected if row.passes())
    baseline_recall = _recall(baseline, denominator)
    baseline_interception = _interception(baseline)
    contributions: list[FilterRuleContribution] = []
    for rule in ABLATION_RULES:
        retained = tuple(row for row in selected if row.passes(frozenset({rule})))
        contributions.append(_contribution(rule, baseline, retained, denominator))
    interactions: list[FilterRuleContribution] = []
    for left, right in interaction_rules:
        if left not in ABLATION_RULES or right not in ABLATION_RULES or left == right:
            raise ValueError("filter ablation interaction is not preregistered")
        retained = tuple(row for row in selected if row.passes(frozenset({left, right})))
        interactions.append(_contribution(f"interaction_{left}_{right}", baseline, retained, denominator, allow_interaction=True))
    recommendations = tuple(
        item.rule for item in contributions if item.profitable_executable_recall is not None and item.profitable_executable_recall > (baseline_recall or 0.0)
    )
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
    )


def _contribution(rule: str, baseline: tuple[FilterAblationRow, ...], retained: tuple[FilterAblationRow, ...], denominator: int, *, allow_interaction: bool = False) -> FilterRuleContribution:
    if not allow_interaction and rule not in ABLATION_RULES:
        raise ValueError("filter ablation rule is not registered")
    p95 = _percentile(tuple(row.latency_ms for row in retained), 0.95)
    return FilterRuleContribution(
        rule=rule,
        baseline_count=len(baseline),
        retained_count=len(retained),
        profitable_executable_recall=_recall(retained, denominator),
        severe_loss_interception=_interception(retained),
        io_saved=max(0, sum(row.io_requests for row in baseline) - sum(row.io_requests for row in retained)),
        scoring_rows_saved=max(0, sum(row.scoring_rows for row in baseline) - sum(row.scoring_rows for row in retained)),
        deepseek_requests_saved=max(0, sum(row.deepseek_requests for row in baseline) - sum(row.deepseek_requests for row in retained)),
        p95_latency_ms=p95,
    )


def _recall(rows: tuple[FilterAblationRow, ...], denominator: int) -> float | None:
    return None if denominator == 0 else sum(row.eligible_profit for row in rows) / denominator


def _interception(rows: tuple[FilterAblationRow, ...]) -> float | None:
    losses = tuple(row for row in rows if row.severe_loss)
    return None if not losses else sum(not row.severe_loss for row in losses) / len(losses)


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


__all__ = ["ABLATION_RULES", "FilterAblationRow", "FilterRuleContribution", "FilterRecallAblationReport", "run_filter_recall_ablation"]
