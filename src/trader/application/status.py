"""Thread-safe runtime status and published snapshot registry."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import datetime

from trader.domain.models import RecommendationSnapshot, Strategy


class RuntimeState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = False
        self._phase = "closed"
        self._last_error = ""
        self._last_tick_at: datetime | None = None
        self._snapshots: dict[Strategy, RecommendationSnapshot] = {}
        self._frozen: set[tuple[Strategy, str]] = set()
        self._counters: dict[str, int] = {
            "ticks": 0,
            "events_submitted": 0,
            "events_completed": 0,
            "events_failed": 0,
            "events_replayed": 0,
            "snapshots_published": 0,
            "snapshots_frozen": 0,
        }

    def mark_started(self, started: bool) -> None:
        with self._lock:
            self._started = started

    def record_tick(self, phase: str, at: datetime) -> None:
        with self._lock:
            self._phase = phase
            self._last_tick_at = at
            self._counters["ticks"] += 1

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def record_error(self, error: str) -> None:
        with self._lock:
            self._last_error = error[:500]

    def publish(self, snapshot: RecommendationSnapshot) -> None:
        with self._lock:
            self._snapshots[snapshot.strategy] = snapshot
            self._counters["snapshots_published"] += 1

    def restore_snapshot(self, snapshot: RecommendationSnapshot) -> None:
        with self._lock:
            self._snapshots[snapshot.strategy] = snapshot

    def mark_frozen(self, snapshot: RecommendationSnapshot) -> None:
        with self._lock:
            self._frozen.add((snapshot.strategy, snapshot.trade_date))
            self._snapshots[snapshot.strategy] = snapshot
            self._counters["snapshots_frozen"] += 1

    def restore_frozen(self, strategy: Strategy, trade_date: str) -> None:
        with self._lock:
            self._frozen.add((strategy, trade_date))

    def latest(self, strategy: Strategy) -> RecommendationSnapshot | None:
        with self._lock:
            return self._snapshots.get(strategy)

    def is_frozen(self, strategy: Strategy, trade_date: str) -> bool:
        with self._lock:
            return (strategy, trade_date) in self._frozen

    def snapshot(self, dependencies: Mapping[str, object] | None = None) -> dict[str, object]:
        with self._lock:
            return {
                "schema_version": "v2",
                "status": "running" if self._started else "stopped",
                "runtime_started": self._started,
                "phase": self._phase,
                "last_tick_at": self._last_tick_at.isoformat() if self._last_tick_at else None,
                "last_error": self._last_error,
                "strategies": {
                    strategy.value: {
                        "snapshot_id": snapshot.snapshot_id,
                        "published_at": snapshot.published_at.isoformat(),
                        "fusion_mode": snapshot.fusion_mode.value,
                        "recommendation_count": len(snapshot.recommendations),
                        "frozen": snapshot.frozen,
                        "stale": snapshot.stale,
                    }
                    for strategy, snapshot in self._snapshots.items()
                },
                "counters": dict(self._counters),
                "dependencies": dict(dependencies or {}),
            }


__all__ = ["RuntimeState"]
