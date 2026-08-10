"""Typed dependency records for the tomorrow shadow runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from trader.application.current_decisions import CurrentDecisionIndex
from trader.application.ports.clock import Clock
from trader.application.ports.snapshots import PublishedSnapshotReadPort, PublishedSnapshotWritePort
from trader.application.ports.tomorrow import TomorrowNativeInput
from trader.application.ports.tomorrow_research import TomorrowResearchTraceRecorderPort
from trader.application.tomorrow_events import TomorrowDecisionEventStream
from trader.application.tomorrow_freezing import TomorrowFreezeCoordinator
from trader.application.tomorrow_shadow import TomorrowCutoverGate
from trader.application.tomorrow_views import (
    TomorrowDecisionQueries,
    TomorrowQuoteOverlayIndex,
)
from trader.domain.recommendation.models import RecommendationSnapshot


class TomorrowShadowProcessor(Protocol):
    def process(self, snapshot: RecommendationSnapshot) -> bool: ...

    def process_native(self, native_input: TomorrowNativeInput) -> bool: ...


class PublishedSnapshotIndexPort(
    PublishedSnapshotReadPort,
    PublishedSnapshotWritePort,
    Protocol,
):
    pass


@dataclass(frozen=True)
class TomorrowShadowDependencies:
    decisions: CurrentDecisionIndex
    quotes: TomorrowQuoteOverlayIndex
    events: TomorrowDecisionEventStream
    queries: TomorrowDecisionQueries
    freezer: TomorrowFreezeCoordinator
    gate: TomorrowCutoverGate
    clock: Clock
    research_trace: TomorrowResearchTraceRecorderPort | None = None


@dataclass(frozen=True)
class NativeProjectionRecord:
    native_input: TomorrowNativeInput
    sequence: int
    local_version: str
    local_publish_seconds: float


__all__ = [
    "NativeProjectionRecord",
    "PublishedSnapshotIndexPort",
    "TomorrowShadowDependencies",
    "TomorrowShadowProcessor",
]
