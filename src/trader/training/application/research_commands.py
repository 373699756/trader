"""Immutable offline research commands passed to the composition root."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trader.training.application.capability_completion import CapabilityExecution
    from trader.training.application.cross_strategy_conclusion import CrossStrategyConclusion
    from trader.training.application.selection_execution import SelectionResearchExecution
    from trader.training.domain.evaluation.historical_industry_facts import HistoricalIndustryDatasetReport
    from trader.training.domain.evaluation.point_in_time_data_qualification import PointInTimeDataQualificationReport

    ResearchEvidenceResult = (
        CapabilityExecution
        | CrossStrategyConclusion
        | HistoricalIndustryDatasetReport
        | PointInTimeDataQualificationReport
        | SelectionResearchExecution
    )


@dataclass(frozen=True)
class CapabilityCommand:
    runtime_dir: Path
    artifact_dir: Path
    code: str
    anchor_date: date
    timeout_seconds: float


@dataclass(frozen=True)
class QualificationCommand:
    history_root: Path
    code: str
    anchor_date: date
    timeout_seconds: float


@dataclass(frozen=True)
class IndustryAuditCommand:
    history_root: Path
    required_sample_codes: int
    tushare_access_points: int


@dataclass(frozen=True)
class HoldoutCommand:
    parent_artifact_dir: Path
    evidence_dir: Path
    output_dir: Path


@dataclass(frozen=True)
class SelectionCommand:
    evidence_dir: Path


ResearchEvidenceCommand = (
    CapabilityCommand | QualificationCommand | IndustryAuditCommand | HoldoutCommand | SelectionCommand
)
