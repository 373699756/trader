"""Typed shared state contract for RecommendationPipeline mixins."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from trader.application.cadence import CadencePlanner, PipelineTask
from trader.application.events import BoundedEventQueue, PipelineEvent
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
    SnapshotReaderPort,
    SnapshotWriterPort,
)
from trader.application.ports.tomorrow import (
    TodayNativeInputPort,
    TomorrowNativeInputPort,
    V2ControlPort,
    V2OverlayPort,
)
from trader.application.publisher import SnapshotPublisher
from trader.application.research_coordination import ResearchCoordinator
from trader.application.status import RuntimeState
from trader.application.trading_session import TradingSessionStatus, TradingSessionTracker
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
    _published_snapshots: PublishedSnapshotWritePort
    _reviews: DeepSeekReviewPort | None
    _tomorrow_native_inputs: TomorrowNativeInputPort | None
    _today_native_inputs: TodayNativeInputPort | None
    _v2_controls: tuple[V2ControlPort, ...]
    _v2_overlays: tuple[V2OverlayPort, ...]
    _live_overlays: dict[tuple[Strategy, str], LiveOverlay]
    _scheduled_inflight: set[PipelineTask]
    _session_snapshot_ids: set[str]
    _company_research_membership_lock: threading.Lock
    _company_research_membership_date: str
    _company_research_membership: set[str]
    _company_research_review_barrier: bool
    _company_research_initial_rescore_pending: bool
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
    _decision_execution_mode: str
    _research_coordinator: ResearchCoordinator
    _trading_session: TradingSessionTracker

    _freshness_status: Callable[..., Mapping[str, object]]
    _submit_overlay_event: Callable[[PipelineEvent], bool]
    _submit_long_quote_event: Callable[[PipelineEvent], bool]

    def _offer_company_research(
        self,
        observed_at: datetime,
        codes: Sequence[str] | None = None,
    ) -> bool:
        raise NotImplementedError

    def _offer_new_recommendation_research(self, observed_at: datetime) -> bool:
        raise NotImplementedError

    def _consume_company_research_review_barrier(self) -> bool:
        raise NotImplementedError

    def _refresh_trading_session(self, at: datetime) -> TradingSessionStatus:
        raise NotImplementedError

    def submit_event(self, event: PipelineEvent) -> bool:
        raise NotImplementedError
