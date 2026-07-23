"""Bounded aggregate latency telemetry for one-process pipeline cycles."""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class _Trace:
    cycle_kind: str
    planned_at: float


class LatencyWaterfall:
    """Collect bounded stage samples without exposing correlation identities."""

    def __init__(
        self,
        *,
        sample_capacity: int = 512,
        trace_capacity: int = 512,
        stage_capacity: int = 64,
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._sample_capacity = max(1, sample_capacity)
        self._trace_capacity = max(1, trace_capacity)
        self._stage_capacity = max(1, stage_capacity)
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._traces: OrderedDict[str, _Trace] = OrderedDict()
        self._stages: OrderedDict[str, deque[float]] = OrderedDict()
        self._planned_count = 0
        self._completed_count = 0
        self._failed_count = 0
        self._timeout_count = 0
        self._superseded_count = 0
        self._dropped_count = 0
        self._dropped_stage_count = 0

    def plan(self, correlation_id: str, cycle_kind: str) -> None:
        normalized_id = correlation_id.strip()
        normalized_kind = cycle_kind.strip()
        if not normalized_id or not normalized_kind:
            raise ValueError("latency correlation_id and cycle_kind must not be empty")
        planned_at = self._monotonic()
        with self._lock:
            if normalized_id in self._traces:
                self._traces.pop(normalized_id)
                self._superseded_count += 1
            if normalized_id not in self._traces and len(self._traces) >= self._trace_capacity:
                self._traces.popitem(last=False)
                self._dropped_count += 1
            self._traces[normalized_id] = _Trace(normalized_kind, planned_at)
            self._traces.move_to_end(normalized_id)
            self._planned_count += 1

    def enter(self, correlation_id: str) -> None:
        entered_at = self._monotonic()
        with self._lock:
            trace = self._traces.get(correlation_id)
            if trace is None:
                return
            self._append_locked("queue_wait", max(0.0, (entered_at - trace.planned_at) * 1000.0))

    def record_duration(self, stage: str, duration_ms: float) -> None:
        normalized_stage = stage.strip()
        if not normalized_stage:
            raise ValueError("latency stage must not be empty")
        value = max(0.0, float(duration_ms))
        if not math.isfinite(value):
            raise ValueError("latency duration must be finite")
        with self._lock:
            self._append_locked(normalized_stage, value)

    def finish(self, correlation_id: str, *, outcome: str) -> None:
        normalized = outcome.strip().lower()
        finished_at = self._monotonic()
        with self._lock:
            trace = self._traces.pop(correlation_id, None)
            if trace is None:
                return
            self._append_locked(
                f"cycle_total:{trace.cycle_kind}",
                max(0.0, (finished_at - trace.planned_at) * 1000.0),
            )
            if normalized == "success":
                self._completed_count += 1
            elif normalized == "timeout":
                self._timeout_count += 1
            elif normalized == "superseded":
                self._superseded_count += 1
            elif normalized == "dropped":
                self._dropped_count += 1
            else:
                self._failed_count += 1

    def status(self) -> Mapping[str, object]:
        with self._lock:
            return {
                "sample_capacity": self._sample_capacity,
                "trace_capacity": self._trace_capacity,
                "stage_capacity": self._stage_capacity,
                "active_trace_count": len(self._traces),
                "planned_count": self._planned_count,
                "completed_count": self._completed_count,
                "failed_count": self._failed_count,
                "timeout_count": self._timeout_count,
                "superseded_count": self._superseded_count,
                "dropped_count": self._dropped_count,
                "dropped_stage_count": self._dropped_stage_count,
                "stages": {name: _summary(tuple(values)) for name, values in sorted(self._stages.items())},
            }

    def _append_locked(self, stage: str, duration_ms: float) -> None:
        samples = self._stages.get(stage)
        if samples is None:
            if len(self._stages) >= self._stage_capacity:
                self._stages.popitem(last=False)
                self._dropped_stage_count += 1
            samples = deque(maxlen=self._sample_capacity)
            self._stages[stage] = samples
        self._stages.move_to_end(stage)
        samples.append(duration_ms)


def _summary(values: tuple[float, ...]) -> dict[str, int | float | None]:
    if not values:
        return {
            "sample_count": 0,
            "p50_ms": None,
            "p95_ms": None,
            "maximum_ms": None,
        }
    ordered = sorted(values)
    return {
        "sample_count": len(ordered),
        "p50_ms": round(ordered[max(0, math.ceil(len(ordered) * 0.50) - 1)], 3),
        "p95_ms": round(ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)], 3),
        "maximum_ms": round(ordered[-1], 3),
    }


__all__ = ["LatencyWaterfall"]
