"""Today terminal holdout artifact store."""

from pathlib import Path

from trader.infra.research.terminal_holdout_artifacts import (
    TerminalHoldoutArtifactConflictError,
    TerminalHoldoutArtifactStore,
)


class TodayTerminalHoldoutArtifactStore(TerminalHoldoutArtifactStore):
    def __init__(self, root: Path):
        super().__init__(root, strategy="today")


__all__ = ["TodayTerminalHoldoutArtifactStore", "TerminalHoldoutArtifactConflictError"]
