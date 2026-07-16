"""Typed pipeline events and bounded coalescing priority queue."""

from __future__ import annotations

import heapq
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
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

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
            "payload": dict(self.payload),
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
        payload=dict(payload) if isinstance(payload, Mapping) else {},
    )


class BoundedEventQueue:
    def __init__(self, *, maximum_size: int, reserved_priority_size: int) -> None:
        self._maximum_size = max(1, maximum_size)
        self._reserved_priority_size = max(1, reserved_priority_size)
        self._condition = threading.Condition()
        self._heap: list[tuple[int, int, str]] = []
        self._events: dict[str, PipelineEvent] = {}
        self._sequence = 0
        self._closed = False
        self._merged_count = 0
        self._rejected_count = 0

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
                self._condition.notify()
                return True

            is_reserved = event.priority <= EventPriority.RISK
            normal_capacity = max(1, self._maximum_size - self._reserved_priority_size)
            normal_count = sum(item.priority > EventPriority.RISK for item in self._events.values())
            if (is_reserved and len(self._events) >= self._maximum_size) or (
                not is_reserved and normal_count >= normal_capacity
            ):
                self._rejected_count += 1
                return False
            self._sequence += 1
            self._events[event.idempotency_key] = event
            heapq.heappush(self._heap, (int(event.priority), self._sequence, event.idempotency_key))
            self._condition.notify()
            return True

    def get(self, timeout_seconds: float | None = None) -> PipelineEvent | None:
        with self._condition:
            if not self._heap and not self._closed:
                self._condition.wait(timeout_seconds)
            while self._heap:
                _priority, _sequence, key = heapq.heappop(self._heap)
                event = self._events.pop(key, None)
                if event is not None:
                    return event
            return None

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def empty(self) -> bool:
        with self._condition:
            return not self._events

    def status(self) -> dict[str, object]:
        with self._condition:
            return {
                "depth": len(self._events),
                "merged_count": self._merged_count,
                "rejected_count": self._rejected_count,
                "closed": self._closed,
            }


__all__ = ["BoundedEventQueue", "EventPriority", "PipelineEvent", "event_from_audit_record", "new_event"]
