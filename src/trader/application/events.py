"""Typed pipeline events and bounded coalescing priority queue."""

from __future__ import annotations

import heapq
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, IntEnum
from types import MappingProxyType
from uuid import uuid4

from trader.application.ports.types import JsonInput, JsonObject, JsonValue, freeze_json_object, thaw_json_value
from trader.domain.recommendation.models import Strategy


class EventPriority(IntEnum):
    FREEZE = 0
    RISK = 10
    DEEPSEEK = 20
    LIVE_QUOTES = 25
    MARKET_QUOTES = 30
    CANDIDATE_QUOTES = 40
    SCORE = 50
    LONG = 60


class EventDeadlineExpiredError(RuntimeError):
    """A non-freeze event exhausted its execution deadline."""


class EventStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    EXPIRED = "expired"


@dataclass(frozen=True)
class EventAuditRecord:
    event_id: str
    event_type: str
    subject_key: str
    trade_date: str
    phase: str
    strategy: str
    priority: int
    data_version: str
    config_version: str
    status: EventStatus
    created_at: datetime
    deadline: datetime | None
    retry_count: int
    payload: JsonObject = field(default_factory=lambda: MappingProxyType({}))
    error: str = ""
    sequence: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", freeze_json_object(self.payload))

    def to_json(self) -> dict[str, JsonInput]:
        return {
            "sequence": self.sequence,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "subject_key": self.subject_key,
            "trade_date": self.trade_date,
            "phase": self.phase,
            "strategy": self.strategy,
            "priority": self.priority,
            "data_version": self.data_version,
            "config_version": self.config_version,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "deadline": self.deadline.isoformat() if self.deadline is not None else "",
            "retry_count": self.retry_count,
            "payload": thaw_json_value(self.payload),
            "error": self.error,
        }


@dataclass(frozen=True)
class PipelineEvent:
    event_id: str
    event_type: str
    subject_key: str
    trade_date: str
    phase: str
    strategy: Strategy | None
    priority: EventPriority
    data_version: str
    config_version: str
    created_at: datetime
    deadline: datetime | None = None
    retry_count: int = 0
    payload: JsonObject = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if self.subject_key != "market" and (len(self.subject_key) != 6 or not self.subject_key.isdigit()):
            raise ValueError("event subject_key must be 'market' or a normalized six-digit stock code")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("event created_at must be timezone-aware")
        if self.deadline is not None and (self.deadline.tzinfo is None or self.deadline.utcoffset() is None):
            raise ValueError("event deadline must be timezone-aware")
        frozen_payload = freeze_json_object(self.payload)
        if self.event_type == "freeze":
            _validate_freeze_event(self, frozen_payload)
        object.__setattr__(self, "payload", frozen_payload)

    @property
    def idempotency_key(self) -> str:
        strategy = self.strategy.value if self.strategy is not None else "shared"
        return ":".join(
            (
                self.trade_date,
                self.phase,
                strategy,
                self.event_type,
                self.subject_key,
                self.data_version,
            )
        )

    def audit_record(self, *, status: EventStatus, error: str = "") -> EventAuditRecord:
        return EventAuditRecord(
            event_id=self.event_id,
            event_type=self.event_type,
            subject_key=self.subject_key,
            trade_date=self.trade_date,
            phase=self.phase,
            strategy=self.strategy.value if self.strategy is not None else "shared",
            priority=int(self.priority),
            data_version=self.data_version,
            config_version=self.config_version,
            status=status,
            created_at=self.created_at,
            deadline=self.deadline,
            retry_count=self.retry_count,
            payload=self.payload,
            error=error,
        )


@dataclass(frozen=True)
class EventSpec:
    event_type: str
    subject_key: str
    trade_date: str
    phase: str
    strategy: Strategy | None
    priority: EventPriority
    data_version: str
    config_version: str
    created_at: datetime
    deadline: datetime | None = None
    payload: Mapping[str, JsonInput] | None = None


def new_event(spec: EventSpec) -> PipelineEvent:
    return PipelineEvent(
        event_id=uuid4().hex,
        event_type=spec.event_type,
        subject_key=spec.subject_key,
        trade_date=spec.trade_date,
        phase=spec.phase,
        strategy=spec.strategy,
        priority=spec.priority,
        data_version=spec.data_version,
        config_version=spec.config_version,
        created_at=spec.created_at,
        deadline=spec.deadline,
        payload=freeze_json_object(spec.payload or {}),
    )


def event_from_audit_record(record: EventAuditRecord) -> PipelineEvent:
    if not isinstance(record.payload, Mapping):
        raise ValueError("persisted event payload must be a mapping")
    strategy_raw = record.strategy
    return PipelineEvent(
        event_id=record.event_id,
        event_type=record.event_type,
        subject_key=record.subject_key,
        trade_date=record.trade_date,
        phase=record.phase,
        strategy=None if strategy_raw == "shared" else Strategy(strategy_raw),
        priority=EventPriority(record.priority),
        data_version=record.data_version,
        config_version=record.config_version,
        created_at=record.created_at,
        deadline=record.deadline,
        retry_count=record.retry_count + 1,
        payload=record.payload,
    )


class BoundedEventQueue:
    def __init__(self, *, maximum_size: int, reserved_priority_size: int) -> None:
        self._maximum_size = max(1, maximum_size)
        self._reserved_priority_size = max(1, reserved_priority_size)
        self._condition = threading.Condition()
        self._heap: list[tuple[int, int, str, str]] = []
        self._events: dict[str, PipelineEvent] = {}
        self._sequence = 0
        self._closed = False
        self._merged_count = 0
        self._rejected_count = 0
        self._replayed_count = 0

    def put(self, event: PipelineEvent) -> bool:
        with self._condition:
            if self._closed:
                self._rejected_count += 1
                return False
            existing = self._events.get(event.idempotency_key)
            if existing is not None:
                if event.created_at <= existing.created_at:
                    self._merged_count += 1
                    return True
                self._events[event.idempotency_key] = event
                self._merged_count += 1
                self._push_locked(event)
                self._condition.notify()
                return True

            is_reserved = event.priority <= EventPriority.RISK
            normal_capacity = max(1, self._maximum_size - self._reserved_priority_size)
            normal_count = sum(item.priority > EventPriority.RISK for item in self._events.values())
            normal_full = normal_count >= normal_capacity or len(self._events) >= self._maximum_size
            if not is_reserved and normal_full:
                if self._replace_older_subject_locked(event):
                    return True
            if (is_reserved and len(self._events) >= self._maximum_size) or (not is_reserved and normal_full):
                self._rejected_count += 1
                return False
            self._events[event.idempotency_key] = event
            self._push_locked(event)
            self._condition.notify()
            return True

    def get(self, timeout_seconds: float | None = None) -> PipelineEvent | None:
        with self._condition:
            if not self._heap and not self._closed:
                self._condition.wait(timeout_seconds)
            while self._heap:
                _priority, _sequence, key, event_id = heapq.heappop(self._heap)
                event = self._events.get(key)
                if event is None or event.event_id != event_id:
                    continue
                self._events.pop(key)
                return event
            return None

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def empty(self) -> bool:
        with self._condition:
            return not self._events

    def record_replayed(self, count: int = 1) -> None:
        with self._condition:
            self._replayed_count += max(0, count)

    def status(self) -> dict[str, JsonValue]:
        with self._condition:
            return {
                "capacity": self._maximum_size,
                "reserved_priority_capacity": self._reserved_priority_size,
                "depth": len(self._events),
                "heap_depth": sum(
                    self._events.get(key) is not None and self._events[key].event_id == event_id
                    for _priority, _sequence, key, event_id in self._heap
                ),
                "heap_storage_depth": len(self._heap),
                "merged_count": self._merged_count,
                "rejected_count": self._rejected_count,
                "replayed_count": self._replayed_count,
                "closed": self._closed,
            }

    def _push_locked(self, event: PipelineEvent) -> None:
        self._sequence += 1
        heapq.heappush(
            self._heap,
            (int(event.priority), self._sequence, event.idempotency_key, event.event_id),
        )
        if len(self._heap) > self._maximum_size * 2:
            self._heap = [
                item
                for item in self._heap
                if (queued := self._events.get(item[2])) is not None and queued.event_id == item[3]
            ]
            heapq.heapify(self._heap)

    def _replace_older_subject_locked(self, event: PipelineEvent) -> bool:
        coalescing_key = _coalescing_key(event)
        matches = tuple(
            (key, queued)
            for key, queued in self._events.items()
            if queued.priority > EventPriority.RISK and _coalescing_key(queued) == coalescing_key
        )
        if matches:
            newest = max((*[queued for _key, queued in matches], event), key=lambda queued: queued.created_at)
            self._merged_count += len(matches)
            for key, _queued in matches:
                self._events.pop(key)
            self._events[newest.idempotency_key] = newest
            if newest is event:
                self._push_locked(event)
            self._condition.notify()
            return True
        return False


def _coalescing_key(event: PipelineEvent) -> tuple[str, str, Strategy | None, str, str]:
    return (
        event.trade_date,
        event.phase,
        event.strategy,
        event.event_type,
        event.subject_key,
    )


def _validate_freeze_event(event: PipelineEvent, payload: JsonObject) -> None:
    freeze_raw = payload.get("freeze_strategies")
    if (
        event.subject_key != "market"
        or event.strategy is not None
        or event.priority is not EventPriority.FREEZE
        or not isinstance(freeze_raw, tuple)
        or not freeze_raw
    ):
        raise ValueError("freeze event requires market subject, freeze priority, and freeze_strategies")
    try:
        strategies = tuple(Strategy(str(value)) for value in freeze_raw)
    except ValueError as exc:
        raise ValueError("freeze_strategies contains an unknown strategy") from exc
    if Strategy.LONG in strategies:
        raise ValueError("freeze_strategies cannot contain long")


__all__ = [
    "BoundedEventQueue",
    "EventAuditRecord",
    "EventDeadlineExpiredError",
    "EventPriority",
    "EventSpec",
    "EventStatus",
    "PipelineEvent",
    "event_from_audit_record",
    "new_event",
]
