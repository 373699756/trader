"""Read and publish the existing immutable research evidence formats."""

from pathlib import Path

from trader.training.application.capability_completion import CapabilityPublication
from trader.training.application.cross_strategy_conclusion import CrossStrategyConclusion
from trader.training.application.h1_point_in_time_completion import H1ResearchCompletion
from trader.training.domain.evaluation.h1_point_in_time import H1CapabilityAuditReport
from trader.training.domain.evaluation.historical_industry_facts import HistoricalIndustryDatasetReport
from trader.training.domain.evaluation.terminal_holdout import TerminalHoldoutParentState
from trader.training.infra.research.cross_strategy_conclusion_artifacts import CrossStrategyConclusionArtifactArchive
from trader.training.infra.research.d25_terminal_holdout_artifacts import D25TerminalHoldoutArtifactArchive
from trader.training.infra.research.h1_point_in_time_capability import H1CapabilityArtifactArchive
from trader.training.infra.research.h1_point_in_time_completion import H1ResearchCompletionArtifactArchive
from trader.training.infra.research.historical_industry_archive import audit_archived_historical_industry_facts
from trader.training.infra.research.historical_label_artifacts import HistoricalLabelArtifactArchive
from trader.training.infra.research.tomorrow_point_in_time_holdout_artifacts import (
    TomorrowPointInTimeHoldoutArtifactArchive,
)


class HistoricalIndustryEvidenceReader:
    def __init__(self, root: Path) -> None:
        self._root = root

    def audit(self, *, required_sample_codes: int, tushare_access_points: int) -> HistoricalIndustryDatasetReport:
        return audit_archived_historical_industry_facts(
            self._root, required_sample_codes=required_sample_codes, tushare_access_points=tushare_access_points
        )


class CapabilityEvidencePublisher:
    def __init__(self, root: Path) -> None:
        self._root = root

    def publish(self, capability: H1CapabilityAuditReport, completion: H1ResearchCompletion) -> CapabilityPublication:
        H1CapabilityArtifactArchive(self._root).write(capability)
        HistoricalLabelArtifactArchive(self._root).write(completion.labels)
        index = H1ResearchCompletionArtifactArchive(self._root).write(completion)
        return CapabilityPublication(
            index.content_hash, index.residual_terminal_hashes, index.daily_close_selection_hash
        )


class TerminalHoldoutParentsReader:
    def __init__(self, root: Path) -> None:
        self._root = root

    def read(self) -> tuple[TerminalHoldoutParentState, TerminalHoldoutParentState]:
        capability = H1CapabilityArtifactArchive(self._root).verify()
        labels = HistoricalLabelArtifactArchive(self._root).verify()
        index = H1ResearchCompletionArtifactArchive(self._root).verify()
        if index.capability_hash != capability.content_hash:
            raise ValueError("Terminal holdout parent capability hash does not match terminal index")
        if index.label_batch_hash != labels.content_hash:
            raise ValueError("Terminal holdout parent label hash does not match terminal index")
        residual_hash = next(value for strategy, value in index.residual_terminal_hashes if strategy == "d25")
        states = tuple(
            TerminalHoldoutParentState(
                candidate_status=status.state,
                parent_hash=capability.content_hash,
                candidate_hash=index.daily_close_selection_hash if status.strategy == "tomorrow" else residual_hash,
                failure_reasons=tuple((*status.failure_reasons, *capability.probe_failures)),
            )
            for status in capability.strategies
        )
        tomorrow = next(
            state for state, status in zip(states, capability.strategies, strict=True) if status.strategy == "tomorrow"
        )
        d25 = next(
            state for state, status in zip(states, capability.strategies, strict=True) if status.strategy == "d25"
        )
        return tomorrow, d25


class TerminalConclusionPublisher:
    def __init__(self, root: Path) -> None:
        self._root = root

    def publish(self, conclusion: CrossStrategyConclusion) -> None:
        TomorrowPointInTimeHoldoutArtifactArchive(self._root / "tomorrow").write(conclusion.tomorrow)
        D25TerminalHoldoutArtifactArchive(self._root / "d25").write(conclusion.d25)
        CrossStrategyConclusionArtifactArchive(self._root / "cross_strategy").write(conclusion)
