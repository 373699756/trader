"""Bounded non-blocking event publication for tomorrow v2."""

from __future__ import annotations

import queue
import threading
from collections import deque
from dataclasses import dataclass
from typing import Literal

from trader.application.tomorrow_views import (
    TomorrowDecisionView,
    TomorrowQuoteOverlay,
)

ResyncReason = Literal[
    "cursor_ahead",
    "cursor_expired",
    "cursor_gap",
    "slow_subscriber",
    "base_mismatch",
    "schema_mismatch",
    "identity_mismatch",
]


@dataclass(frozen=True)
class TomorrowDecisionEventPayload:
    trade_date: str
    projection_version: str
    decision_version: str
    market_epoch_version: str
    feature_epoch_version: str | None
    research_epoch_version: str | None
    quote_version: str | None
    config_version: str
    strategy_version: str
    fusion_version: str
    projection_stage: str
    frozen: bool
    freeze_version: str | None
    etag: str


@dataclass(frozen=True)
class TomorrowOverlayEventPayload:
    decision_version: str
    projection_version: str
    quote_version: str
    overlay: TomorrowQuoteOverlay


@dataclass(frozen=True)
class TomorrowResyncEventPayload:
    reason: ResyncReason
    projection_version: str | None


TomorrowEventPayload = TomorrowDecisionEventPayload | TomorrowOverlayEventPayload | TomorrowResyncEventPayload


@dataclass(frozen=True)
class TomorrowPublishedEvent:
    sequence: int
    event_type: Literal["decision", "quote_overlay", "resync_required"]
    payload: TomorrowEventPayload


@dataclass(frozen=True)
class TomorrowEventPublishResult:
    accepted: bool
    reason: str
    event: TomorrowPublishedEvent | None = None


@dataclass(frozen=True)
class TomorrowSubscription:
    queue: queue.Queue[TomorrowPublishedEvent]
    replay: tuple[TomorrowPublishedEvent, ...]
    server_sequence_at_open: int
    resync_reason: ResyncReason | None


@dataclass(frozen=True)
class TomorrowEventStreamStatus:
    sequence: int
    history_size: int
    subscriber_count: int
    slow_subscriber_drops: int


class TomorrowSubscriberLimitError(RuntimeError):
    """The bounded event stream has no free subscriber slot."""


class TomorrowDecisionEventStream:
    def __init__(
        self,
        *,
        history_size: int = 256,
        client_queue_size: int = 16,
        subscriber_limit: int = 32,
    ) -> None:
        if min(history_size, client_queue_size, subscriber_limit) < 1:
            raise ValueError("tomorrow event stream bounds must be positive")
        self._lock = threading.RLock()
        self._history: deque[TomorrowPublishedEvent] = deque(maxlen=history_size)
        self._client_queue_size = client_queue_size
        self._subscriber_limit = subscriber_limit
        self._subscribers: set[queue.Queue[TomorrowPublishedEvent]] = set()
        self._sequence = 0
        self._projection_version: str | None = None
        self._trade_date: str | None = None
        self._selected_codes: frozenset[str] = frozenset()
        self._slow_subscriber_drops = 0

    def publish_decision(self, view: TomorrowDecisionView) -> TomorrowPublishedEvent:
        payload = _decision_payload(view)
        with self._lock:
            self._projection_version = payload.projection_version
            self._trade_date = payload.trade_date
            self._selected_codes = frozenset(item.code for item in view.items)
            return self._publish_locked("decision", payload)

    def publish_overlay(self, overlay: TomorrowQuoteOverlay) -> TomorrowEventPublishResult:
        with self._lock:
            if self._projection_version != overlay.decision_version:
                event = self._publish_locked(
                    "resync_required",
                    TomorrowResyncEventPayload("identity_mismatch", self._projection_version),
                )
                return TomorrowEventPublishResult(False, "identity_mismatch", event)
            if overlay.observed_at.date().isoformat() != self._trade_date or any(
                item.code not in self._selected_codes for item in overlay.quotes
            ):
                event = self._publish_locked(
                    "resync_required",
                    TomorrowResyncEventPayload("base_mismatch", self._projection_version),
                )
                return TomorrowEventPublishResult(False, "base_mismatch", event)
            event = self._publish_locked(
                "quote_overlay",
                TomorrowOverlayEventPayload(
                    decision_version=overlay.decision_version,
                    projection_version=overlay.decision_version,
                    quote_version=overlay.version,
                    overlay=overlay,
                ),
            )
            return TomorrowEventPublishResult(True, "accepted", event)

    def open_subscription(self, after_sequence: int | None) -> TomorrowSubscription:
        if after_sequence is not None and after_sequence < 0:
            raise ValueError("tomorrow event cursor cannot be negative")
        subscriber: queue.Queue[TomorrowPublishedEvent] = queue.Queue(maxsize=self._client_queue_size)
        with self._lock:
            if len(self._subscribers) >= self._subscriber_limit:
                raise TomorrowSubscriberLimitError("tomorrow event subscriber limit reached")
            sequence = self._sequence
            cursor = sequence if after_sequence is None else after_sequence
            replay, reason = self._replay_locked(cursor)
            self._subscribers.add(subscriber)
            return TomorrowSubscription(subscriber, replay, sequence, reason)

    def unsubscribe(self, subscriber: queue.Queue[TomorrowPublishedEvent]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def is_subscribed(self, subscriber: queue.Queue[TomorrowPublishedEvent]) -> bool:
        with self._lock:
            return subscriber in self._subscribers

    def last_sequence(self) -> int:
        with self._lock:
            return self._sequence

    def status(self) -> TomorrowEventStreamStatus:
        with self._lock:
            return TomorrowEventStreamStatus(
                sequence=self._sequence,
                history_size=len(self._history),
                subscriber_count=len(self._subscribers),
                slow_subscriber_drops=self._slow_subscriber_drops,
            )

    def _publish_locked(
        self,
        event_type: Literal["decision", "quote_overlay", "resync_required"],
        payload: TomorrowEventPayload,
    ) -> TomorrowPublishedEvent:
        self._sequence += 1
        event = TomorrowPublishedEvent(self._sequence, event_type, payload)
        self._history.append(event)
        dropped: list[queue.Queue[TomorrowPublishedEvent]] = []
        for subscriber in self._subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                dropped.append(subscriber)
        for subscriber in dropped:
            self._subscribers.discard(subscriber)
        self._slow_subscriber_drops += len(dropped)
        return event

    def _replay_locked(
        self,
        after_sequence: int,
    ) -> tuple[tuple[TomorrowPublishedEvent, ...], ResyncReason | None]:
        if after_sequence > self._sequence:
            return (), "cursor_ahead"
        if after_sequence == self._sequence:
            return (), None
        if not self._history:
            return (), "cursor_expired"
        first = self._history[0].sequence
        if after_sequence < first - 1:
            return (), "cursor_expired"
        replay = tuple(event for event in self._history if event.sequence > after_sequence)
        expected = tuple(range(after_sequence + 1, self._sequence + 1))
        if tuple(event.sequence for event in replay) != expected:
            return (), "cursor_gap"
        return replay, None


def _decision_payload(view: TomorrowDecisionView) -> TomorrowDecisionEventPayload:
    required = (
        view.trade_date,
        view.projection_version,
        view.decision_version,
        view.market_epoch_version,
        view.config_version,
        view.strategy_version,
        view.fusion_version,
        view.projection_stage,
        view.etag,
    )
    if view.status != "ready" or any(value is None for value in required):
        raise ValueError("tomorrow decision event requires a complete ready identity")
    return TomorrowDecisionEventPayload(
        trade_date=view.trade_date or "",
        projection_version=view.projection_version or "",
        decision_version=view.decision_version or "",
        market_epoch_version=view.market_epoch_version or "",
        feature_epoch_version=view.feature_epoch_version,
        research_epoch_version=view.research_epoch_version,
        quote_version=view.quote_version,
        config_version=view.config_version or "",
        strategy_version=view.strategy_version or "",
        fusion_version=view.fusion_version or "",
        projection_stage=view.projection_stage or "",
        frozen=view.frozen,
        freeze_version=view.freeze_version,
        etag=view.etag or "",
    )


__all__ = [
    "ResyncReason",
    "TomorrowDecisionEventPayload",
    "TomorrowDecisionEventStream",
    "TomorrowEventPublishResult",
    "TomorrowEventStreamStatus",
    "TomorrowOverlayEventPayload",
    "TomorrowPublishedEvent",
    "TomorrowResyncEventPayload",
    "TomorrowSubscriberLimitError",
    "TomorrowSubscription",
]
