"""D25 terminal holdout report publication."""

from pathlib import Path

from trader.training.infra.research.terminal_holdout_reports import (
    TerminalHoldoutReportConflictError,
    TerminalHoldoutReportIndex,
)


class D25TerminalHoldoutReportIndex(TerminalHoldoutReportIndex):
    def __init__(self, root: Path):
        super().__init__(root, strategy="d25")


__all__ = ["D25TerminalHoldoutReportIndex", "TerminalHoldoutReportConflictError"]
