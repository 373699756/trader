"""Preregistered, human-readable candidate families for historical research."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.domain.research.filter_recall_ablation import (
    ABLATION_RULES,
    FilterAblationRow,
    FilterRecallAblationReport,
    FilterRuleId,
)

CandidateChangeKind = Literal["control", "observe_rule", "remove_component", "cost_guard"]
_IDENTITY = re.compile(r"^[a-z0-9_]{1,96}$")


@dataclass(frozen=True)
class TransparentCandidate:
    candidate_id: str
    strategy: str
    change_kind: CandidateChangeKind
    disabled_rules: frozenset[FilterRuleId] = frozenset()
    removed_component: str | None = None
    cost_rate: float = 0.002
    severe_loss_guard: float | None = None
    rationale: str = ""
    production_authority: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if _IDENTITY.fullmatch(self.candidate_id) is None or not self.strategy:
            raise ValueError("transparent candidate identity is invalid")
        if self.change_kind not in {"control", "observe_rule", "remove_component", "cost_guard"}:
            raise ValueError("transparent candidate change kind is invalid")
        if any(rule not in ABLATION_RULES for rule in self.disabled_rules):
            raise ValueError("transparent candidate disabled rule is not preregistered")
        if self.change_kind == "control" and (self.disabled_rules or self.removed_component or self.severe_loss_guard is not None):
            raise ValueError("control candidate cannot change the production chain")
        if self.change_kind == "observe_rule" and len(self.disabled_rules) != 1:
            raise ValueError("observe candidate must disable exactly one rule")
        if self.change_kind == "remove_component" and (not self.removed_component or self.disabled_rules):
            raise ValueError("component candidate must name exactly one component")
        if self.change_kind == "cost_guard" and (self.severe_loss_guard is None or self.disabled_rules or self.removed_component):
            raise ValueError("cost guard candidate requires one fixed guard")
        if not math.isfinite(self.cost_rate) or self.cost_rate not in {0.002, 0.005, 0.01}:
            raise ValueError("transparent candidate cost must be 20bp, 50bp, or 100bp")
        if self.severe_loss_guard is not None and (not math.isfinite(self.severe_loss_guard) or self.severe_loss_guard < 0.0):
            raise ValueError("transparent candidate severe-loss guard is invalid")
        if self.production_authority:
            raise ValueError("transparent candidates cannot authorize production")
        object.__setattr__(self, "content_hash", _hash(self))


@dataclass(frozen=True)
class TransparentCandidateMetrics:
    candidate_id: str
    evaluated_rows: int
    evaluated_dates: int
    net_excess_20bp: float
    net_excess_50bp: float
    severe_loss_rate: float
    turnover: float
    capacity: float
    concentration: float
    profitable_executable_recall: float | None
    implementation_complexity: int

    def __post_init__(self) -> None:
        if _IDENTITY.fullmatch(self.candidate_id) is None or min(self.evaluated_rows, self.evaluated_dates, self.implementation_complexity) < 0:
            raise ValueError("transparent candidate metrics identity/count is invalid")
        values = (self.net_excess_20bp, self.net_excess_50bp, self.severe_loss_rate, self.turnover, self.capacity, self.concentration)
        if any(not math.isfinite(value) for value in values):
            raise ValueError("transparent candidate metrics must be finite")
        if not 0.0 <= self.severe_loss_rate <= 1.0 or not 0.0 <= self.concentration <= 1.0:
            raise ValueError("transparent candidate rates must be in [0, 1]")
        if self.profitable_executable_recall is not None and not 0.0 <= self.profitable_executable_recall <= 1.0:
            raise ValueError("transparent candidate recall must be in [0, 1]")


@dataclass(frozen=True)
class TransparentCandidateFamily:
    strategy: str
    candidates: tuple[TransparentCandidate, ...]
    source_ablation_hash: str
    development_dates: tuple[date, ...]
    confirmation_locked: bool = False
    production_authority: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if not self.strategy or not self.candidates or len(self.candidates) > 8:
            raise ValueError("transparent candidate family must contain one to eight candidates")
        if self.candidates[0].change_kind != "control":
            raise ValueError("transparent candidate family must start with its control")
        if len({candidate.candidate_id for candidate in self.candidates}) != len(self.candidates):
            raise ValueError("transparent candidate ids must be unique")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_ablation_hash) or not self.development_dates:
            raise ValueError("transparent candidate family identity is invalid")
        if self.confirmation_locked and self.production_authority:
            raise ValueError("locked research family cannot authorize production")
        if self.production_authority:
            raise ValueError("transparent candidate family cannot authorize production")
        object.__setattr__(self, "content_hash", _hash(self))


def preregister_transparent_candidates(
    report: FilterRecallAblationReport,
    *,
    removed_components: tuple[str, ...] = (),
    include_cost_guard: bool = True,
) -> TransparentCandidateFamily:
    """Build at most eight candidates from the sealed development ablation."""

    candidates: list[TransparentCandidate] = [TransparentCandidate(f"{report.strategy}_control", report.strategy, "control")]
    for rule in report.recommendations:
        if len(candidates) >= 8:
            break
        if rule in ABLATION_RULES:
            candidates.append(TransparentCandidate(f"{report.strategy}_observe_{rule}", report.strategy, "observe_rule", frozenset({rule}), rationale="ablation_recall"))
    for component in removed_components:
        if len(candidates) >= 8:
            break
        if not component.strip():
            raise ValueError("removed component must be named")
        candidates.append(TransparentCandidate(f"{report.strategy}_remove_{component}", report.strategy, "remove_component", removed_component=component, rationale="stable_zero_component"))
    if include_cost_guard and len(candidates) < 8:
        candidates.append(TransparentCandidate(f"{report.strategy}_cost_guard", report.strategy, "cost_guard", severe_loss_guard=0.10, rationale="registered_cost_guard"))
    return TransparentCandidateFamily(report.strategy, tuple(candidates), report.content_hash, report.development_dates)


def evaluate_transparent_candidate(
    candidate: TransparentCandidate,
    rows: tuple[FilterAblationRow, ...],
    *,
    control: TransparentCandidateMetrics | None = None,
) -> TransparentCandidateMetrics:
    """Evaluate one fixed candidate without fitting or searching parameters."""

    if not rows:
        raise ValueError("transparent candidate evaluation requires rows")
    eligible = tuple(row for row in rows if row.passes(candidate.disabled_rules))
    if candidate.severe_loss_guard is not None:
        eligible = tuple(row for row in eligible if not row.severe_loss or row.actual_net_excess_20bp >= -candidate.severe_loss_guard)
    returns = tuple(row.actual_net_excess_20bp if candidate.cost_rate == 0.002 else row.actual_net_excess_50bp for row in eligible)
    returns_50 = tuple(row.actual_net_excess_50bp for row in eligible)
    severe = tuple(row.severe_loss for row in eligible)
    denominator = sum(row.eligible_profit for row in rows)
    recall = None if denominator == 0 else sum(row.eligible_profit for row in eligible) / denominator
    return TransparentCandidateMetrics(
        candidate.candidate_id,
        len(eligible),
        len({row.trade_date for row in eligible}),
        math.fsum(returns) / len(returns) if returns else 0.0,
        math.fsum(returns_50) / len(returns_50) if returns_50 else 0.0,
        sum(severe) / len(severe) if severe else 0.0,
        math.fsum(row.latency_ms for row in eligible) / len(eligible) if eligible else 0.0,
        1.0 / (1.0 + math.fsum(row.io_requests for row in eligible)),
        _concentration(eligible),
        recall,
        1 if candidate.change_kind == "control" else 2,
    )


def choose_development_candidate(metrics: tuple[TransparentCandidateMetrics, ...]) -> str | None:
    """Choose a sole candidate only when it clears fixed risk and concentration gates."""

    if not metrics or metrics[0].candidate_id == "":
        return None
    control = metrics[0]
    eligible = tuple(item for item in metrics[1:] if item.net_excess_20bp > control.net_excess_20bp and item.net_excess_50bp > control.net_excess_50bp and item.severe_loss_rate <= control.severe_loss_rate and item.concentration <= control.concentration)
    return max(eligible, key=lambda item: (item.net_excess_20bp, item.net_excess_50bp, -item.implementation_complexity, item.candidate_id)).candidate_id if eligible else control.candidate_id


def _concentration(rows: tuple[FilterAblationRow, ...]) -> float:
    if not rows:
        return 0.0
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.industry] = counts.get(row.industry, 0) + 1
    return max(counts.values()) / len(rows)


def _hash(value: object) -> str:
    def encode(item: object) -> object:
        if dataclasses.is_dataclass(item):
            return {field.name: encode(getattr(item, field.name)) for field in dataclasses.fields(item) if field.init}
        if isinstance(item, (date,)):
            return item.isoformat()
        if isinstance(item, (tuple, list, frozenset, set)):
            return [encode(child) for child in item]
        if isinstance(item, dict):
            return {str(key): encode(child) for key, child in item.items()}
        return item
    payload = json.dumps(encode(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


__all__ = ["CandidateChangeKind", "TransparentCandidate", "TransparentCandidateMetrics", "TransparentCandidateFamily", "preregister_transparent_candidates", "evaluate_transparent_candidate", "choose_development_candidate"]
