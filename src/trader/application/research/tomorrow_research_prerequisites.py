"""Bridge Codex A point-in-time label readiness into Tomorrow orchestration."""

from __future__ import annotations

from typing import Protocol

from trader.application.research.research_tomorrow_orchestrator import TomorrowResearchPrerequisite
from trader.domain.research.historical_label import HistoricalLabelPreregistrationBatch


class HistoricalLabelPreregistrationPort(Protocol):
    def execute(self) -> HistoricalLabelPreregistrationBatch: ...


class CodexATomorrowResearchPrerequisite:
    def __init__(self, labels: HistoricalLabelPreregistrationPort) -> None:
        self._labels = labels

    def inspect(self) -> TomorrowResearchPrerequisite:
        batch = self._labels.execute()
        tomorrow = next(item for item in batch.strategies if item.strategy == "tomorrow")
        blockers = tuple(f"tomorrow_{reason}" for reason in tomorrow.failure_reasons)
        return TomorrowResearchPrerequisite(
            status="ready" if tomorrow.status == "preregistered" else "blocked",
            prerequisite_hash=batch.content_hash,
            blockers=blockers,
        )


__all__ = ["CodexATomorrowResearchPrerequisite", "HistoricalLabelPreregistrationPort"]
