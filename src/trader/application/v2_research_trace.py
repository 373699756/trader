"""Bounded research trace projection keyed by the production V2 decision identity."""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass

from trader.application.decision_events import V2DecisionCommitted


@dataclass(frozen=True)
class V2ResearchTraceStatus:
    retained: int
    recorded: int
    duplicate: int


class InMemoryV2ResearchTraceStore:
    """Observer consumer; it stores the committed event without a baseline identity."""

    def __init__(self, *, capacity: int = 2048) -> None:
        if capacity < 1:
            raise ValueError("V2 research trace capacity must be positive")
        self._capacity = capacity
        self._lock = threading.Lock()
        self._records: OrderedDict[str, V2DecisionCommitted] = OrderedDict()
        self._recorded = 0
        self._duplicate = 0

    def record(self, event: V2DecisionCommitted) -> None:
        with self._lock:
            current = self._records.get(event.decision_version)
            if current is not None:
                if current != event:
                    raise ValueError("V2 research trace identity conflict")
                self._duplicate += 1
                return
            self._records[event.decision_version] = event
            self._records.move_to_end(event.decision_version)
            while len(self._records) > self._capacity:
                self._records.popitem(last=False)
            self._recorded += 1

    def get(self, decision_version: str) -> V2DecisionCommitted | None:
        with self._lock:
            return self._records.get(decision_version)

    def status(self) -> V2ResearchTraceStatus:
        with self._lock:
            return V2ResearchTraceStatus(len(self._records), self._recorded, self._duplicate)


__all__ = ["InMemoryV2ResearchTraceStore", "V2ResearchTraceStatus"]
