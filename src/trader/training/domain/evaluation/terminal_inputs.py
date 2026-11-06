"""Pure identities and pre-read admission for terminal outcome partitions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.training.domain.evaluation.research_plan import ResearchExecutionPlan
from trader.training.domain.evaluation.terminal_holdout import TerminalHoldoutParentState, TerminalStrategy


@dataclass(frozen=True)
class TerminalHoldoutPartition:
    strategy: TerminalStrategy
    parent_hash: str
    candidate_hash: str
    trade_dates: tuple[date, ...]
    rows_sha256: str
    row_count: int

    def __post_init__(self) -> None:
        if (
            self.strategy not in {"tomorrow", "d25"}
            or any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in (self.parent_hash, self.candidate_hash, self.rows_sha256))
            or not self.trade_dates or self.trade_dates != tuple(sorted(set(self.trade_dates)))
            or len(self.trade_dates) > 20_000 or type(self.row_count) is not int
            or not len(self.trade_dates) <= self.row_count <= 2_000_000
        ):
            raise ValueError("terminal partition metadata is invalid")


@dataclass(frozen=True)
class TerminalHoldoutEvidence:
    research_plan_hash: str
    tomorrow: TerminalHoldoutPartition
    d25: TerminalHoldoutPartition

    @property
    def content_hash(self) -> str:
        return CanonicalDigest(self)

    def validate(
        self,
        plan: ResearchExecutionPlan,
        parents: tuple[TerminalHoldoutParentState, TerminalHoldoutParentState],
    ) -> None:
        if self.research_plan_hash != plan.content_hash or (self.tomorrow.strategy, self.d25.strategy) != ("tomorrow", "d25"):
            raise ValueError("terminal evidence does not match the configured research plan")
        for partition, parent in zip((self.tomorrow, self.d25), parents, strict=True):
            if parent.ready:
                plan.validate_terminal_parent(partition.strategy, parent)
                if parent.date_split is None or partition.trade_dates != parent.date_split.terminal_holdout_dates:
                    raise ValueError("terminal dates differ from the sealed unseen parent partition")
            if (
                partition.parent_hash != parent.parent_hash or partition.candidate_hash != parent.candidate_hash
                or len(partition.trade_dates) < plan.settings.terminal_holdout_sessions
            ):
                raise ValueError("terminal evidence does not match its sealed parents or date budget")
