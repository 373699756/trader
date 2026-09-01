"""D25 14:50 point-in-time terminal holdout adapter."""

from __future__ import annotations

from collections.abc import Sequence

from trader.domain.research.terminal_holdout import TerminalHoldoutReport, TerminalHoldoutRow, evaluate_terminal_holdout

D25TerminalRow = TerminalHoldoutRow


class D25TerminalHoldoutService:
    def __init__(
        self,
        rows: Sequence[D25TerminalRow],
        *,
        candidate_status: str = "historical_candidate_ready",
        parent_hash: str = "0" * 64,
        candidate_hash: str = "1" * 64,
        parent_failure_reasons: tuple[str, ...] = (),
    ) -> None:
        self._rows = tuple(rows)
        self._candidate_status = candidate_status
        self._parent_hash = parent_hash
        self._candidate_hash = candidate_hash
        self._parent_failure_reasons = parent_failure_reasons

    def execute(self) -> TerminalHoldoutReport:
        return evaluate_terminal_holdout(
            strategy="d25",
            research_identity="score_d25_historical_candidate_v1",
            parent_hash=self._parent_hash,
            candidate_hash=self._candidate_hash,
            rows=self._rows,
            parent_status=self._candidate_status,
            parent_failure_reasons=self._parent_failure_reasons,
            anchor="14:50_unadjusted_point_in_time",
            bootstrap_block_days=10,
        )


__all__ = ["D25TerminalHoldoutReport", "D25TerminalHoldoutService", "D25TerminalRow"]
D25TerminalHoldoutReport = TerminalHoldoutReport
