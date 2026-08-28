"""Pure cost-aware ranking and portfolio constraints for shadow research."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from trader.domain.research.tomorrow_features import ResearchBoard

CostAwareHorizon = Literal["tomorrow", "d25"]
COST_AWARE_UTILITY_FIELDS = ("gross_expected_excess", "estimated_cost")

_TOMORROW_ENTRY_THRESHOLD = 0.0
_D25_ENTRY_THRESHOLD = 0.002
_D25_MAINTENANCE_THRESHOLD = 0.0


@dataclass(frozen=True)
class CostAwareCandidate:
    code: str
    board: ResearchBoard
    industry: str
    gross_expected_excess: float
    estimated_cost: float
    severe_loss_probability: float
    uncertainty: float
    incumbent: bool = False

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit():
            raise ValueError("cost-aware candidate code must contain exactly six digits")
        if self.board not in ("main", "chinext", "star"):
            raise ValueError("cost-aware candidate board is invalid")
        object.__setattr__(self, "industry", self.industry.strip() or "unknown")
        for value in (self.gross_expected_excess, self.estimated_cost, self.uncertainty):
            if not math.isfinite(value):
                raise ValueError("cost-aware candidate values must be finite")
        if self.estimated_cost < 0.0 or self.uncertainty < 0.0:
            raise ValueError("cost and uncertainty must be non-negative")
        if not math.isfinite(self.severe_loss_probability) or not 0.0 < self.severe_loss_probability < 1.0:
            raise ValueError("severe-loss probability must be strictly between zero and one")

    @property
    def net_utility(self) -> float:
        return self.gross_expected_excess - self.estimated_cost


@dataclass(frozen=True)
class CostAwareSelectionPolicy:
    horizon: CostAwareHorizon
    top_k: int = 6
    maximum_per_industry: int = 2
    maximum_board_fraction: float = 0.60
    entry_threshold: float = field(init=False)
    maintenance_threshold: float = field(init=False)

    def __post_init__(self) -> None:
        if self.horizon not in ("tomorrow", "d25"):
            raise ValueError("cost-aware horizon is invalid")
        if self.top_k != 6 or self.maximum_per_industry != 2 or self.maximum_board_fraction != 0.60:
            raise ValueError("cost-aware portfolio constraints are fixed")
        if self.horizon == "d25":
            entry = _D25_ENTRY_THRESHOLD
            maintenance = _D25_MAINTENANCE_THRESHOLD
        else:
            entry = _TOMORROW_ENTRY_THRESHOLD
            maintenance = _TOMORROW_ENTRY_THRESHOLD
        object.__setattr__(self, "entry_threshold", entry)
        object.__setattr__(self, "maintenance_threshold", maintenance)


@dataclass(frozen=True)
class CostAwareEvaluation:
    code: str
    board: ResearchBoard
    industry: str
    gross_expected_excess: float
    estimated_cost: float
    net_utility: float
    severe_loss_probability: float
    uncertainty: float
    incumbent: bool
    required_threshold: float
    selected_rank: int | None
    skip_reason: str


@dataclass(frozen=True)
class CostAwareSelectionResult:
    evaluations: tuple[CostAwareEvaluation, ...]
    selected_codes: tuple[str, ...]


def select_cost_aware(
    candidates: tuple[CostAwareCandidate, ...],
    policy: CostAwareSelectionPolicy,
) -> CostAwareSelectionResult:
    """Rank by post-cost utility, then apply the frozen portfolio constraints."""

    codes = tuple(item.code for item in candidates)
    if len(codes) != len(set(codes)):
        raise ValueError("cost-aware candidates must contain unique codes")
    if policy.horizon == "tomorrow" and any(item.incumbent for item in candidates):
        raise ValueError("Tomorrow cannot carry incumbent state across its fixed holding period")
    ordered = tuple(sorted(candidates, key=_candidate_order))
    eligible = tuple(item for item in ordered if item.net_utility >= _threshold(item, policy))
    selected = _constrained_selection(eligible, policy)
    selected_ranks = {item.code: rank for rank, item in enumerate(selected, start=1)}
    evaluations = tuple(
        CostAwareEvaluation(
            code=item.code,
            board=item.board,
            industry=item.industry,
            gross_expected_excess=item.gross_expected_excess,
            estimated_cost=item.estimated_cost,
            net_utility=item.net_utility,
            severe_loss_probability=item.severe_loss_probability,
            uncertainty=item.uncertainty,
            incumbent=item.incumbent,
            required_threshold=_threshold(item, policy),
            selected_rank=selected_ranks.get(item.code),
            skip_reason=_skip_reason(item, policy, selected_ranks),
        )
        for item in ordered
    )
    return CostAwareSelectionResult(evaluations, tuple(item.code for item in selected))


def _threshold(candidate: CostAwareCandidate, policy: CostAwareSelectionPolicy) -> float:
    return policy.maintenance_threshold if candidate.incumbent else policy.entry_threshold


def _candidate_order(candidate: CostAwareCandidate) -> tuple[float, float, float, str]:
    return (-candidate.net_utility, candidate.severe_loss_probability, candidate.uncertainty, candidate.code)


def _constrained_selection(
    eligible: tuple[CostAwareCandidate, ...],
    policy: CostAwareSelectionPolicy,
) -> tuple[CostAwareCandidate, ...]:
    for target_size in range(min(policy.top_k, len(eligible)), 1, -1):
        board_limit = math.floor(policy.maximum_board_fraction * target_size)
        selected: list[CostAwareCandidate] = []
        board_counts: dict[ResearchBoard, int] = {}
        industry_counts: dict[str, int] = {}
        for candidate in eligible:
            if board_counts.get(candidate.board, 0) >= board_limit:
                continue
            if industry_counts.get(candidate.industry, 0) >= policy.maximum_per_industry:
                continue
            selected.append(candidate)
            board_counts[candidate.board] = board_counts.get(candidate.board, 0) + 1
            industry_counts[candidate.industry] = industry_counts.get(candidate.industry, 0) + 1
            if len(selected) == target_size:
                return tuple(selected)
    return ()


def _skip_reason(
    candidate: CostAwareCandidate,
    policy: CostAwareSelectionPolicy,
    selected_ranks: dict[str, int],
) -> str:
    if candidate.code in selected_ranks:
        return ""
    if candidate.net_utility < _threshold(candidate, policy):
        return "maintenance_threshold" if candidate.incumbent else "entry_threshold"
    return "portfolio_constraint"


__all__ = [
    "COST_AWARE_UTILITY_FIELDS",
    "CostAwareCandidate",
    "CostAwareEvaluation",
    "CostAwareHorizon",
    "CostAwareSelectionPolicy",
    "CostAwareSelectionResult",
    "select_cost_aware",
]
