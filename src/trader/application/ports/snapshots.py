"""Snapshot read/write and observability ports."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, TypeAlias

from trader.application.ports.types import JsonObject
from trader.domain.recommendation.models import LiveOverlay, RecommendationSnapshot, Strategy

SnapshotStatusValue: TypeAlias = object


@dataclass(frozen=True)
class RecoverySummary:
    recovered: int = 0
    quarantined: int = 0
    orphaned: int = 0


class SnapshotReaderPort(Protocol):
    def load_frozen(self, strategy: Strategy, trade_date: str) -> RecommendationSnapshot | None: ...

    def recommendation_dates(self, strategy: Strategy) -> Sequence[str]: ...

    def load_live_overlay(self, strategy: Strategy, trade_date: str) -> LiveOverlay | None: ...


class SnapshotWriterPort(Protocol):
    def initialize(self) -> None: ...

    def freeze(self, snapshot: RecommendationSnapshot) -> None: ...

    def save_checkpoint(self, snapshot: RecommendationSnapshot, *, boundary_at: datetime) -> None: ...

    def load_checkpoint(
        self, strategy: Strategy, trade_date: str, *, boundary_at: datetime
    ) -> RecommendationSnapshot | None: ...

    def consume_checkpoint(self, strategy: Strategy, trade_date: str, *, boundary_at: datetime) -> None: ...

    def save_live_overlay(self, overlay: LiveOverlay) -> bool: ...

    def recover(self) -> RecoverySummary: ...


class SnapshotObservabilityPort(Protocol):
    def record_data_source_health(self, health: JsonObject, *, updated_at: datetime) -> None: ...

    def observability_status(self) -> JsonObject: ...


@dataclass(frozen=True)
class SnapshotPorts:
    reader: SnapshotReaderPort
    writer: SnapshotWriterPort
    observability: SnapshotObservabilityPort


class CurrentSnapshotReaderPort(Protocol):
    def latest(self, strategy: Strategy) -> RecommendationSnapshot | None: ...

    def load_live_overlay(self, strategy: Strategy, trade_date: str) -> LiveOverlay | None: ...


class PublishedSnapshotReadPort(CurrentSnapshotReaderPort, Protocol):
    """P6 read model used by HTTP; hot reads never touch persistence."""

    def load_frozen(self, strategy: Strategy, trade_date: str) -> RecommendationSnapshot | None: ...

    def recommendation_dates(self, strategy: Strategy) -> Sequence[str]: ...

    def status(self) -> Mapping[str, SnapshotStatusValue]: ...


class PublishedSnapshotWritePort(Protocol):
    def publish(self, snapshot: RecommendationSnapshot) -> None: ...

    def publish_overlay(self, overlay: LiveOverlay) -> None: ...

    def status(self) -> Mapping[str, SnapshotStatusValue]: ...
