"""Tomorrow terminal holdout artifact store."""

from pathlib import Path

from trader.infra.research.terminal_holdout_artifacts import (
    TerminalHoldoutArtifactConflictError,
    TerminalHoldoutArtifactStore,
)


class TomorrowPointInTimeHoldoutArtifactStore(TerminalHoldoutArtifactStore):
    def __init__(self, root: Path):
        super().__init__(root, strategy="tomorrow")


__all__ = ["TomorrowPointInTimeHoldoutArtifactStore", "TerminalHoldoutArtifactConflictError"]
