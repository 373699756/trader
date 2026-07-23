"""Typed shared state contract for RecommendationPipeline mixins."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from datetime import datetime

from trader.application.cadence import CadencePlanner, PipelineTask
from trader.application.events import BoundedEventQueue
from trader.application.latency import LatencyWaterfall
from trader.application.ports.clock import TradingCalendarPort
from trader.application.ports.events import EventAuditPort
from trader.application.ports.market import (
    CandidateFeatureReaderPort,
    FullMarketReaderPort,
    MarketMetadataPort,
    QuoteReaderPort,
    ReferenceDataPort,
    ResearchReaderPort,
)
from trader.application.ports.reviews import DeepSeekReviewPort
from trader.application.ports.snapshots import (
    PublishedSnapshotWritePort,
    SnapshotObservabilityPort,
    SnapshotReaderPort,
    SnapshotWriterPort,
)
from trader.application.publisher import SnapshotPublisher
from trader.application.status import RuntimeState
from trader.application.workers import BoundedExecutor
from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.models import (
    LiveOverlay,
    Strategy,
)


class PipelineState:
    _market_full: FullMarketReaderPort
    _candidate_data: CandidateFeatureReaderPort
    _quotes: QuoteReaderPort
    _research: ResearchReaderPort
    _references: ReferenceDataPort
    _market_metadata: MarketMetadataPort
    _calendar: TradingCalendarPort
    _event_audit: EventAuditPort
    _publisher: SnapshotPublisher
    _state: RuntimeState
    _queue: BoundedEventQueue
    _cadence: CadencePlanner | None
    _candidate_codes: tuple[str, ...]
    _now: Callable[[], datetime]
    _config_version: str
    _repository: SnapshotReaderPort
    _snapshot_writer: SnapshotWriterPort
    _snapshot_observability: SnapshotObservabilityPort
    _published_snapshots: PublishedSnapshotWritePort
    _reviews: DeepSeekReviewPort | None
    _live_overlays: dict[tuple[Strategy, str], LiveOverlay]
    _scheduled_inflight: set[PipelineTask]
    _session_snapshot_ids: set[str]
    _after_close_completed_date: str
    _after_close_retry_at: datetime | None
    _after_close_retry_attempt: int
    _market_features: tuple[FeatureSnapshot, ...]
    _lifecycle_lock: threading.Lock
    _cadence_lock: threading.Lock
    _worker: threading.Thread | None
    _stopped: bool
    _accepting: bool
    _persistence_running: bool
    _persistence_pool: BoundedExecutor
    _latency: LatencyWaterfall

    _freshness_status: Callable[..., Mapping[str, object]]
