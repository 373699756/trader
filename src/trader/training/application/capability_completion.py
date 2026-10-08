"""Execute and publish the fail-closed H1 capability completion through typed ports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from trader.training.application.h1_point_in_time_completion import H1ResearchCompletion, complete_h1_research
from trader.training.domain.evaluation.h1_point_in_time import H1CapabilityAuditReport, H1PointInTimeSpec
from trader.training.domain.evaluation.historical_label import H1CoverageMetadata


class CapabilityProbePort(Protocol):
    def run(self, *, code: str, historical_anchor_date: date) -> H1CapabilityAuditReport: ...


class LabelMetadataPort(Protocol):
    def label_metadata(self, spec: H1PointInTimeSpec) -> H1CoverageMetadata: ...


@dataclass(frozen=True)
class CapabilityPublication:
    terminal_index_hash: str
    residual_terminal_hashes: tuple[tuple[str, str], ...]
    daily_close_selection_hash: str


class CompletionPublicationPort(Protocol):
    def publish(
        self, capability: H1CapabilityAuditReport, completion: H1ResearchCompletion
    ) -> CapabilityPublication: ...


@dataclass(frozen=True)
class CapabilityExecution:
    capability: H1CapabilityAuditReport
    completion: H1ResearchCompletion
    publication: CapabilityPublication


class CompleteCapabilityEvidence:
    def __init__(
        self, probe: CapabilityProbePort, metadata: LabelMetadataPort, publisher: CompletionPublicationPort
    ) -> None:
        self._probe = probe
        self._metadata = metadata
        self._publisher = publisher

    def execute(self, *, code: str, historical_anchor_date: date) -> CapabilityExecution:
        capability = self._probe.run(code=code, historical_anchor_date=historical_anchor_date)
        metadata = tuple(self._metadata.label_metadata(H1PointInTimeSpec(s)) for s in ("tomorrow", "d25"))
        completion = complete_h1_research(capability=capability, metadata=metadata)
        publication = self._publisher.publish(capability, completion)
        return CapabilityExecution(capability, completion, publication)
