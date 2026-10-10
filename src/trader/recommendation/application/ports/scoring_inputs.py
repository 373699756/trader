"""Immutable accepted input batches exposed to decision construction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from trader.recommendation.application.pipeline.stage_output import PipelineStageOutput
from trader.recommendation.application.ports.read_only_queries import InputQualityStatus
from trader.recommendation.application.ports.runtime import CycleRequest
from trader.recommendation.domain.evidence.pipeline import PipelineStageSnapshot
from trader.recommendation.domain.market.eligibility import IssuerEligibilityBatch
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.selection.scored_selection import ScoredCandidateStageCounts, SelectedCandidateInput


@dataclass(frozen=True)
class InputBatch:
    request: CycleRequest
    market_features: tuple[FeatureSnapshot, ...]
    requested_codes: tuple[str, ...]
    candidate_features: tuple[FeatureSnapshot, ...]
    data_version: str
    candidate_stage_counts: ScoredCandidateStageCounts | None = None
    candidate_quote_eligible: int = 0
    preselection_transient_invalid: bool = False
    issuer_eligibility: IssuerEligibilityBatch | None = None
    input_stages: tuple[PipelineStageSnapshot, ...] = ()
    candidate_stage: PipelineStageOutput[SelectedCandidateInput] | None = None


@dataclass(frozen=True)
class TopKQuoteBatch:
    observed_at: datetime
    features: tuple[FeatureSnapshot, ...]


class AcceptedScoringInputs(Protocol):
    def batch(self, request: CycleRequest) -> InputBatch | None: ...
    def topk_batch(self) -> TopKQuoteBatch | None: ...
    def quality(self, strategy: Strategy) -> InputQualityStatus | None: ...
    def replace_quality(
        self, strategy: Strategy, expected: InputQualityStatus | None, quality: InputQualityStatus
    ) -> bool: ...
    def input_quality_status(self) -> tuple[InputQualityStatus, ...]: ...
