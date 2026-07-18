"""Typed pipeline events and bounded coalescing priority queue."""

from __future__ import annotations

import heapq
import math
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from types import MappingProxyType
from uuid import uuid4

from trader.domain.models import Strategy


class EventPriority(IntEnum):
    FREEZE = 0
    RISK = 10
    DEEPSEEK = 20
    SCORE = 30
    CANDIDATE_QUOTES = 40
    MARKET_QUOTES = 50
    LONG = 60


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
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.subject_key != "market" and (len(self.subject_key) != 6 or not self.subject_key.isdigit()):
            raise ValueError("event subject_key must be 'market' or a normalized six-digit stock code")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("event created_at must be timezone-aware")
        if self.deadline is not None and (self.deadline.tzinfo is None or self.deadline.utcoffset() is None):
            raise ValueError("event deadline must be timezone-aware")
        frozen_payload = MappingProxyType({key: _freeze_payload_value(value) for key, value in self.payload.items()})
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

    def audit_record(self, *, status: str, error: str = "") -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "subject_key": self.subject_key,
            "trade_date": self.trade_date,
            "phase": self.phase,
            "strategy": self.strategy.value if self.strategy is not None else "shared",
            "priority": int(self.priority),
            "data_version": self.data_version,
            "config_version": self.config_version,
            "status": status,
            "created_at": self.created_at.isoformat(),
            "deadline": self.deadline.isoformat() if self.deadline is not None else "",
            "retry_count": self.retry_count,
            "payload": _thaw_payload_value(self.payload),
            "error": error,
        }


def new_event(
    event_type: str,
    *,
    subject_key: str,
    trade_date: str,
    phase: str,
    strategy: Strategy | None,
    priority: EventPriority,
    data_version: str,
    config_version: str,
    created_at: datetime,
    deadline: datetime | None = None,
    payload: Mapping[str, object] | None = None,
) -> PipelineEvent:
    return PipelineEvent(
        event_id=uuid4().hex,
        event_type=event_type,
        subject_key=subject_key,
        trade_date=trade_date,
        phase=phase,
        strategy=strategy,
        priority=priority,
        data_version=data_version,
        config_version=config_version,
        created_at=created_at,
        deadline=deadline,
        payload=payload or {},
    )


def event_from_audit_record(record: Mapping[str, object]) -> PipelineEvent:
    strategy_raw = str(record.get("strategy") or "shared")
    payload = record.get("payload")
    deadline_raw = str(record.get("deadline") or "")
    retry_raw = record.get("retry_count", 0)
    priority_raw = record.get("priority")
    if not isinstance(retry_raw, int) or isinstance(retry_raw, bool):
        raise ValueError("persisted event retry_count must be an integer")
    if not isinstance(priority_raw, int) or isinstance(priority_raw, bool):
        raise ValueError("persisted event priority must be an integer")
    if not isinstance(payload, Mapping):
        raise ValueError("persisted event payload must be a mapping")
    return PipelineEvent(
        event_id=str(record["event_id"]),
        event_type=str(record["event_type"]),
        subject_key=str(record["subject_key"]),
        trade_date=str(record["trade_date"]),
        phase=str(record["phase"]),
        strategy=None if strategy_raw == "shared" else Strategy(strategy_raw),
        priority=EventPriority(priority_raw),
        data_version=str(record["data_version"]),
        config_version=str(record["config_version"]),
        created_at=datetime.fromisoformat(str(record["created_at"])),
        deadline=datetime.fromisoformat(deadline_raw) if deadline_raw else None,
        retry_count=retry_raw + 1,
        payload=dict(payload),
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

    def status(self) -> dict[str, object]:
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


def _coalescing_key(event: PipelineEvent) -> tuple[object, ...]:
    return (
        event.trade_date,
        event.phase,
        event.strategy,
        event.event_type,
        event.subject_key,
    )


def _freeze_payload_value(value: object) -> object:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("event payload mapping keys must be strings")
        return MappingProxyType({str(key): _freeze_payload_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_payload_value(item) for item in value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("event payload floats must be finite")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError("event payload values must be JSON-compatible")


def _validate_freeze_event(event: PipelineEvent, payload: Mapping[str, object]) -> None:
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


def _thaw_payload_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_payload_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_payload_value(item) for item in value]
    return value


__all__ = ["BoundedEventQueue", "EventPriority", "PipelineEvent", "event_from_audit_record", "new_event"]
