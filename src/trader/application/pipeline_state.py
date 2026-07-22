"""Typed shared state contract for RecommendationPipeline mixins."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from trader.application.cadence import CadencePlanner, PipelineTask
from trader.application.events import BoundedEventQueue
from trader.application.ports import EventAuditPort, MarketDataPort, TradingCalendarPort
from trader.application.publisher import SnapshotPublisher
from trader.application.status import RuntimeState
from trader.application.workers import BoundedExecutor
from trader.domain.models import FeatureSnapshot, LiveOverlay, Strategy


class PipelineState:
    _market_data: MarketDataPort
    _calendar: TradingCalendarPort
    _event_audit: EventAuditPort
    _publisher: SnapshotPublisher
    _state: RuntimeState
    _queue: BoundedEventQueue
    _cadence: CadencePlanner | None
    _candidate_codes: tuple[str, ...]
    _now: Callable[[], datetime]
    _config_version: str
    _repository: Any
    _reviews: Any
    _live_overlays: dict[tuple[Strategy, str], LiveOverlay]
    _scheduled_inflight: set[PipelineTask]
    _session_snapshot_ids: set[str]
    _after_close_completed_date: str
    _after_close_retry_at: datetime | None
    _after_close_retry_attempt: int
    _market_features: tuple[FeatureSnapshot, ...]
    _lifecycle_lock: Any
    _cadence_lock: Any
    _worker: threading.Thread | None
    _stopped: bool
    _accepting: bool
    _persistence_running: bool
    _persistence_pool: BoundedExecutor

    _freshness_status: Callable[..., Mapping[str, object]]
