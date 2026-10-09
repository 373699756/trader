"""Exact offline TopK oracle under crossing board and industry limits."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations

from trader.training.domain.evaluation.historical import SUPPORTED_RESEARCH_BOARDS, ResearchBoard


@dataclass(frozen=True)
class ConstrainedOracleCandidate:
    code: str
    board: ResearchBoard
    industry: str
    net_return: float

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit() or self.board not in SUPPORTED_RESEARCH_BOARDS:
            raise ValueError("constrained oracle candidate identity is invalid")
        if not self.industry.strip() or not math.isfinite(self.net_return):
            raise ValueError("constrained oracle candidate outcome is invalid")
        object.__setattr__(self, "industry", self.industry.strip())


@dataclass(frozen=True)
class ConstrainedOraclePolicy:
    top_k: int
    max_per_board: int
    max_per_industry: int

    def __post_init__(self) -> None:
        if not 1 <= self.top_k <= 6 or not 1 <= self.max_per_board <= self.top_k or not 1 <= self.max_per_industry <= 2:
            raise ValueError("constrained oracle policy exceeds the bounded research limits")


@dataclass(frozen=True)
class ConstrainedOracleResult:
    net_return_sum: float
    selected_codes: tuple[str, ...]


def constrained_oracle(
    candidates: tuple[ConstrainedOracleCandidate, ...], policy: ConstrainedOraclePolicy
) -> ConstrainedOracleResult:
    if len({item.code for item in candidates}) != len(candidates):
        raise ValueError("constrained oracle requires unique candidate codes")
    industries = tuple(sorted({item.industry for item in candidates}))
    states: dict[tuple[int, ...], tuple[Decimal, tuple[str, ...]]] = {(0, 0, 0): (Decimal(0), ())}
    for industry in industries:
        options = _industry_options(candidates, industry, policy)
        updated = dict(states)
        for counts, (total, codes) in states.items():
            for selected in options:
                next_counts = tuple(
                    count + sum(item.board == board for item in selected)
                    for count, board in zip(counts, SUPPORTED_RESEARCH_BOARDS, strict=True)
                )
                if sum(next_counts) > policy.top_k or any(count > policy.max_per_board for count in next_counts):
                    continue
                next_total = total + sum((Decimal(str(item.net_return)) for item in selected), start=Decimal(0))
                next_codes = tuple(sorted((*codes, *(item.code for item in selected))))
                previous = updated.get(next_counts)
                if (
                    previous is None
                    or next_total > previous[0]
                    or (next_total == previous[0] and next_codes < previous[1])
                ):
                    updated[next_counts] = (next_total, next_codes)
        states = updated
    total, codes = min(states.values(), key=lambda item: (-item[0], item[1]))
    return ConstrainedOracleResult(float(total), codes)


def _industry_options(
    candidates: tuple[ConstrainedOracleCandidate, ...], industry: str, policy: ConstrainedOraclePolicy
) -> tuple[tuple[ConstrainedOracleCandidate, ...], ...]:
    # Identical board/industry exposure makes all but the best cap-sized subset dominated.
    pool = tuple(
        item
        for board in SUPPORTED_RESEARCH_BOARDS
        for item in sorted(
            (
                item
                for item in candidates
                if item.industry == industry and item.board == board and item.net_return > 0.0
            ),
            key=lambda item: (-item.net_return, item.code),
        )[: min(policy.max_per_industry, policy.max_per_board, policy.top_k)]
    )
    return tuple(
        option for size in range(1, min(policy.max_per_industry, len(pool)) + 1) for option in combinations(pool, size)
    )


__all__ = ["ConstrainedOracleCandidate", "ConstrainedOraclePolicy", "ConstrainedOracleResult", "constrained_oracle"]
