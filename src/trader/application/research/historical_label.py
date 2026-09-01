"""Read-only orchestration for historical label and split preregistration."""

from __future__ import annotations

from typing import Protocol

from trader.domain.research.h1_point_in_time import H1PointInTimeSpec
from trader.domain.research.historical_label import (
    H1CoverageMetadata,
    HistoricalLabelPreregistrationBatch,
    preregister_historical_labels,
)


class H1CoverageMetadataPort(Protocol):
    def label_metadata(self, spec: H1PointInTimeSpec) -> H1CoverageMetadata: ...


class HistoricalLabelPreregistrationService:
    def __init__(self, metadata: H1CoverageMetadataPort) -> None:
        self._metadata = metadata

    def execute(self) -> HistoricalLabelPreregistrationBatch:
        values = tuple(
            self._metadata.label_metadata(H1PointInTimeSpec(strategy)) for strategy in ("today", "tomorrow", "d25")
        )
        return preregister_historical_labels(values)


__all__ = ["H1CoverageMetadataPort", "HistoricalLabelPreregistrationService"]
