"""Tomorrow 14:50 point-in-time terminal holdout adapter."""

from __future__ import annotations

from collections.abc import Sequence

from trader.domain.research.terminal_holdout import (
    TerminalHoldoutEvaluation,
    TerminalHoldoutParentState,
    TerminalHoldoutReport,
    TerminalHoldoutRow,
    evaluate_terminal_holdout,
)

TomorrowPointInTimeRow = TerminalHoldoutRow
_DEFAULT_PARENT_STATE = TerminalHoldoutParentState()


class TomorrowPointInTimeHoldoutService:
    def __init__(
        self,
        rows: Sequence[TomorrowPointInTimeRow],
        parent: TerminalHoldoutParentState = _DEFAULT_PARENT_STATE,
    ) -> None:
        self._rows = tuple(rows)
        self._parent = parent

    def execute(self) -> TerminalHoldoutReport:
        return evaluate_terminal_holdout(
            TerminalHoldoutEvaluation(
                strategy="tomorrow",
                research_identity="score_tomorrow_historical_candidate",
                parent_hash=self._parent.parent_hash,
                candidate_hash=self._parent.candidate_hash,
                rows=self._rows,
                parent_status=self._parent.candidate_status,
                parent_failure_reasons=self._parent.failure_reasons,
                anchor="14:50_unadjusted_point_in_time",
                bootstrap_block_days=5,
                terminal_holdout_already_opened=self._parent.already_opened,
            )
        )


__all__ = ["TomorrowPointInTimeHoldoutReport", "TomorrowPointInTimeHoldoutService", "TomorrowPointInTimeRow"]
TomorrowPointInTimeHoldoutReport = TerminalHoldoutReport
