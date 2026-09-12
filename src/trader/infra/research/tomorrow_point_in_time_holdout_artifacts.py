"""Tomorrow terminal holdout artifact archive."""

from pathlib import Path

from trader.infra.research.terminal_holdout_artifacts import (
    TerminalHoldoutArtifactArchive,
    TerminalHoldoutArtifactConflictError,
)


class TomorrowPointInTimeHoldoutArtifactArchive(TerminalHoldoutArtifactArchive):
    def __init__(self, root: Path):
        super().__init__(root, strategy="tomorrow")


__all__ = ["TomorrowPointInTimeHoldoutArtifactArchive", "TerminalHoldoutArtifactConflictError"]
