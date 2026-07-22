"""Thread-safe runtime status and published snapshot registry."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import datetime

from trader.domain.recommendation.models import (
    LiveOverlay,
    RecommendationSnapshot,
    Strategy,
)


class RuntimeState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = False
        self._phase = "closed"
        self._last_error = ""
        self._last_tick_at: datetime | None = None
        self._snapshots: dict[Strategy, RecommendationSnapshot] = {}
        self._live_overlays: dict[tuple[Strategy, str], LiveOverlay] = {}
        self._strategy_degraded_reasons: dict[Strategy, tuple[str, ...]] = {}
        self._frozen: set[tuple[Strategy, str]] = set()
        self._strategy_latency_ms: dict[Strategy, float] = {}
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

    def record_strategy_latency(self, strategy: Strategy, latency_ms: float) -> None:
        with self._lock:
            self._strategy_latency_ms[strategy] = max(0.0, float(latency_ms))

    def record_strategy_degraded(self, strategy: Strategy, reasons: tuple[str, ...]) -> None:
        with self._lock:
            self._strategy_degraded_reasons[strategy] = tuple(dict.fromkeys(reasons))

    def publish(self, snapshot: RecommendationSnapshot) -> None:
        with self._lock:
            self._snapshots[snapshot.strategy] = snapshot
            self._discard_mismatched_overlay(snapshot)
            self._strategy_degraded_reasons.pop(snapshot.strategy, None)
            self._counters["snapshots_published"] += 1

    def restore_snapshot(self, snapshot: RecommendationSnapshot) -> None:
        with self._lock:
            self._snapshots[snapshot.strategy] = snapshot
            self._discard_mismatched_overlay(snapshot)

    def mark_frozen(self, snapshot: RecommendationSnapshot) -> None:
        with self._lock:
            self._frozen.add((snapshot.strategy, snapshot.trade_date))
            self._snapshots[snapshot.strategy] = snapshot
            self._discard_mismatched_overlay(snapshot)
            self._counters["snapshots_frozen"] += 1

    def restore_frozen(self, strategy: Strategy, trade_date: str) -> None:
        with self._lock:
            self._frozen.add((strategy, trade_date))

    def latest(self, strategy: Strategy) -> RecommendationSnapshot | None:
        with self._lock:
            return self._snapshots.get(strategy)

    def publish_overlay(self, overlay: LiveOverlay) -> None:
        with self._lock:
            snapshot = self._snapshots.get(overlay.strategy)
            if (
                snapshot is not None
                and snapshot.trade_date == overlay.trade_date
                and snapshot.snapshot_id == overlay.snapshot_id
            ):
                self._live_overlays[(overlay.strategy, overlay.trade_date)] = overlay

    def restore_overlay(self, overlay: LiveOverlay) -> None:
        self.publish_overlay(overlay)

    def load_live_overlay(self, strategy: Strategy, trade_date: str) -> LiveOverlay | None:
        with self._lock:
            return self._live_overlays.get((strategy, trade_date))

    def _discard_mismatched_overlay(self, snapshot: RecommendationSnapshot) -> None:
        stale_keys = tuple(
            key
            for key, overlay in self._live_overlays.items()
            if key[0] is snapshot.strategy
            and (key[1] != snapshot.trade_date or overlay.snapshot_id != snapshot.snapshot_id)
        )
        for key in stale_keys:
            self._live_overlays.pop(key, None)

    def is_frozen(self, strategy: Strategy, trade_date: str) -> bool:
        with self._lock:
            return (strategy, trade_date) in self._frozen

    def current_phase(self) -> str:
        with self._lock:
            return self._phase

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
                        "phase": snapshot.phase,
                        "data_version": snapshot.data_version,
                        "strategy_version": snapshot.strategy_version,
                        "config_version": snapshot.config_version,
                        "candidate_count": _metadata_integer(snapshot.metadata, "candidate_count"),
                        "filtered_count": snapshot.filtered_count,
                        "filter_reasons": dict(snapshot.filter_reasons),
                        "score_latency_ms": self._strategy_latency_ms.get(strategy),
                        "topk_count": len(snapshot.recommendations),
                        "veto_count": sum(item.veto for item in snapshot.recommendations),
                        "freeze_anchor": snapshot.metadata.get("freeze_anchor", {}),
                        "runtime_degraded_reasons": self._strategy_degraded_reasons.get(strategy, ()),
                    }
                    for strategy, snapshot in self._snapshots.items()
                },
                "strategy_degraded_reasons": {
                    strategy.value: reasons for strategy, reasons in self._strategy_degraded_reasons.items()
                },
                "counters": dict(self._counters),
                "dependencies": dict(dependencies or {}),
            }


def _metadata_integer(metadata: Mapping[str, object], key: str) -> int:
    value = metadata.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


__all__ = ["RuntimeState"]
