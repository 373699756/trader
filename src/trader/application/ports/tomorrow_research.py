"""Application-owned port for non-blocking tomorrow research traces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from trader.application.tomorrow_research_trace_types import (
    TomorrowResearchTrace,
    TomorrowResearchTraceCapture,
    TomorrowResearchTraceRecorderStatus,
)

ResearchTraceEnqueueStatus = Literal["queued", "queue_full"]


@dataclass(frozen=True)
class TomorrowResearchTraceEnqueueResult:
    status: ResearchTraceEnqueueStatus
    identity: str
    payload_bytes: int


class TomorrowResearchTraceRecorderPort(Protocol):
    def enqueue(self, capture: TomorrowResearchTraceCapture) -> TomorrowResearchTraceEnqueueResult: ...

    def get(self, input_version: str) -> TomorrowResearchTrace | None: ...

    def status(self) -> TomorrowResearchTraceRecorderStatus: ...


__all__ = [
    "ResearchTraceEnqueueStatus",
    "TomorrowResearchTraceEnqueueResult",
    "TomorrowResearchTraceRecorderPort",
]
