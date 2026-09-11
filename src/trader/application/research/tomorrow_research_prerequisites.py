"""Bridge point-in-time label readiness into Tomorrow orchestration."""

from __future__ import annotations

from typing import Protocol

from trader.application.research.tomorrow_research_orchestrator import TomorrowResearchPrerequisiteStatus
from trader.domain.research.historical_label import HistoricalLabelPreregistrationBatch


class HistoricalLabelPreregistrationPort(Protocol):
    def execute(self) -> HistoricalLabelPreregistrationBatch: ...


class TomorrowLabelReadinessInspector:
    def __init__(self, labels: HistoricalLabelPreregistrationPort) -> None:
        self._labels = labels

    def inspect(self) -> TomorrowResearchPrerequisiteStatus:
        batch = self._labels.execute()
        tomorrow = next(item for item in batch.strategies if item.strategy == "tomorrow")
        blockers = tuple(f"tomorrow_{reason}" for reason in tomorrow.failure_reasons)
        return TomorrowResearchPrerequisiteStatus(
            status="ready" if tomorrow.status == "preregistered" else "blocked",
            prerequisite_hash=batch.content_hash,
            blockers=blockers,
        )


__all__ = ["HistoricalLabelPreregistrationPort", "TomorrowLabelReadinessInspector"]
