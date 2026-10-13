"""Tomorrow terminal holdout report publication."""

from pathlib import Path

from trader.training.infra.research.terminal_holdout_reports import (
    TerminalHoldoutReportConflictError,
    TerminalHoldoutReportIndex,
)


class TomorrowPointInTimeHoldoutReportIndex(TerminalHoldoutReportIndex):
    def __init__(self, root: Path):
        super().__init__(root, strategy="tomorrow")


__all__ = ["TomorrowPointInTimeHoldoutReportIndex", "TerminalHoldoutReportConflictError"]
