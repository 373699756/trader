"""One isolated selection-study use case; resource construction belongs to bootstrap."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from datetime import datetime

from trader.training.application.selection_experiment import (
    PairedSelectionExperimentBuilder,
    SelectionExperimentEvidencePort,
    SelectionExperimentPrerequisite,
)
from trader.training.domain.evaluation.selection_experiment import (
    PairedSelectionExperimentReport,
    PairedSelectionSpecification,
)
from trader.training.domain.evaluation.selection_statistics import EvaluateSelectionStatistics, SelectionInference
from trader.training.domain.evaluation.research_plan import ResearchExecutionPlan


@dataclass(frozen=True)
class SelectionResearchExecution:
    specification: PairedSelectionSpecification
    prerequisite_hash: str
    report: PairedSelectionExperimentReport
    inference: tuple[SelectionInference, ...]


class SelectionResearchPlanPort(SelectionExperimentEvidencePort, Protocol):
    def load_plan(self) -> tuple[PairedSelectionSpecification, SelectionExperimentPrerequisite]: ...


def ExecuteSelectionResearch(evidence: SelectionResearchPlanPort, plan: ResearchExecutionPlan, *, evaluation_at: datetime) -> SelectionResearchExecution:
    specification, prerequisite = evidence.load_plan()
    plan.validate_selection(specification)
    if specification.registration is not None:
        specification.registration.validate_before_read(evaluation_at)
    report = PairedSelectionExperimentBuilder(evidence).build(specification, prerequisite)
    return SelectionResearchExecution(
        specification, prerequisite.content_hash, report, EvaluateSelectionStatistics(specification, report)
    )


__all__ = ["ExecuteSelectionResearch", "SelectionResearchExecution", "SelectionResearchPlanPort"]
