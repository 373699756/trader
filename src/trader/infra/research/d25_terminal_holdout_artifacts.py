"""D25 terminal holdout artifact archive."""

from pathlib import Path

from trader.infra.research.terminal_holdout_artifacts import (
    TerminalHoldoutArtifactArchive,
    TerminalHoldoutArtifactConflictError,
)


class D25TerminalHoldoutArtifactArchive(TerminalHoldoutArtifactArchive):
    def __init__(self, root: Path):
        super().__init__(root, strategy="d25")


__all__ = ["D25TerminalHoldoutArtifactArchive", "TerminalHoldoutArtifactConflictError"]
