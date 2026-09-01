"""D25 14:50 point-in-time terminal holdout adapter."""

from __future__ import annotations

from collections.abc import Sequence

from trader.domain.research.terminal_holdout import (
    TerminalHoldoutEvaluation,
    TerminalHoldoutParentState,
    TerminalHoldoutReport,
    TerminalHoldoutRow,
    evaluate_terminal_holdout,
)

D25TerminalRow = TerminalHoldoutRow


class D25TerminalHoldoutService:
    def __init__(
        self,
        rows: Sequence[D25TerminalRow],
        parent: TerminalHoldoutParentState = TerminalHoldoutParentState(),
    ) -> None:
        self._rows = tuple(rows)
        self._parent = parent

    def execute(self) -> TerminalHoldoutReport:
        return evaluate_terminal_holdout(
            TerminalHoldoutEvaluation(
                strategy="d25",
                research_identity="score_d25_historical_candidate_v1",
                parent_hash=self._parent.parent_hash,
                candidate_hash=self._parent.candidate_hash,
                rows=self._rows,
                parent_status=self._parent.candidate_status,
                parent_failure_reasons=self._parent.failure_reasons,
                anchor="14:50_unadjusted_point_in_time",
                bootstrap_block_days=10,
                terminal_holdout_already_opened=self._parent.already_opened,
            )
        )


__all__ = ["D25TerminalHoldoutReport", "D25TerminalHoldoutService", "D25TerminalRow"]
D25TerminalHoldoutReport = TerminalHoldoutReport
