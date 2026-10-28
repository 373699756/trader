"""Immutable accepted input batches exposed to freeze construction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from trader.recommendation.application.pipeline.stage_output import PipelineStageOutput
from trader.recommendation.application.ports.read_only_queries import InputQualityStatus
from trader.recommendation.application.ports.runtime import CycleRequest
from trader.recommendation.application.runtime.trading_session import SessionLease
from trader.recommendation.domain.evidence.pipeline import PipelineStageSnapshot
from trader.recommendation.domain.market.eligibility import IssuerEligibilityBatch
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.selection.scored_selection import ScoredCandidateStageCounts, SelectedCandidateInput


@dataclass(frozen=True)
class ScoringInputIdentity:
    strategy: Strategy
    trade_date: date
    source_revision: int
    input_version: str
    generation: int
    sequence: int


@dataclass(frozen=True)
class InputQualityObservation:
    quality: InputQualityStatus
    identity: ScoringInputIdentity | None = None
    parent_version: str | None = None


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
    quality_identity: ScoringInputIdentity | None = None
    started_monotonic: float | None = None


@dataclass(frozen=True)
class TopKQuoteBatch:
    observed_at: datetime
    features: tuple[FeatureSnapshot, ...]


class AcceptedScoringInputs(Protocol):
    def batch(self, request: CycleRequest) -> InputBatch | None: ...
    def topk_batch(self) -> TopKQuoteBatch | None: ...
    def quality(self, strategy: Strategy) -> InputQualityObservation | None: ...
    def replace_quality(
        self,
        identity: ScoringInputIdentity,
        expected: InputQualityObservation | None,
        quality: InputQualityStatus,
        *,
        parent_version: str,
        session: SessionLease | None,
        valid_before: datetime | None = None,
    ) -> bool: ...
    def input_quality_status(self) -> tuple[InputQualityStatus, ...]: ...
