"""D25 terminal holdout artifact store."""

from pathlib import Path

from trader.infra.research.terminal_holdout_artifacts import TerminalHoldoutArtifactConflictError, TerminalHoldoutArtifactStore


class D25TerminalHoldoutArtifactStore(TerminalHoldoutArtifactStore):
    def __init__(self, root: Path):
        super().__init__(root, strategy="d25")


__all__ = ["D25TerminalHoldoutArtifactStore", "TerminalHoldoutArtifactConflictError"]
