"""Today 11:20 point-in-time terminal holdout adapter."""

from __future__ import annotations

from collections.abc import Sequence

from trader.domain.research.terminal_holdout import (
    TerminalHoldoutEvaluation,
    TerminalHoldoutParentState,
    TerminalHoldoutReport,
    TerminalHoldoutRow,
    evaluate_terminal_holdout,
)

TodayTerminalRow = TerminalHoldoutRow
_DEFAULT_PARENT_STATE = TerminalHoldoutParentState()


class TodayTerminalHoldoutService:
    def __init__(
        self,
        rows: Sequence[TodayTerminalRow],
        parent: TerminalHoldoutParentState = _DEFAULT_PARENT_STATE,
    ) -> None:
        self._rows = tuple(rows)
        self._parent = parent

    def execute(self) -> TerminalHoldoutReport:
        return evaluate_terminal_holdout(
            TerminalHoldoutEvaluation(
                strategy="today",
                research_identity="score_today_historical_candidate_v1",
                parent_hash=self._parent.parent_hash,
                candidate_hash=self._parent.candidate_hash,
                rows=self._rows,
                parent_status=self._parent.candidate_status,
                parent_failure_reasons=self._parent.failure_reasons,
                anchor="11:20_unadjusted_point_in_time",
                bootstrap_block_days=5,
                terminal_holdout_already_opened=self._parent.already_opened,
            )
        )


__all__ = ["TodayTerminalHoldoutReport", "TodayTerminalHoldoutService", "TodayTerminalRow"]
TodayTerminalHoldoutReport = TerminalHoldoutReport
