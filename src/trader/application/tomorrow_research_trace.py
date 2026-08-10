"""Bounded, idempotent storage and asynchronous submission for research traces."""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import Mapping
from concurrent.futures import Future
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Literal, TypeAlias

from trader.application.ports.tomorrow_research import TomorrowResearchTraceEnqueueResult
from trader.application.tomorrow_research_projection import build_tomorrow_research_trace
from trader.application.tomorrow_research_trace_types import (
    TomorrowResearchTrace,
    TomorrowResearchTraceCapture,
    TomorrowResearchTraceRecorderStatus,
)
from trader.application.workers import WorkerExecutor

TraceRecordStatus = Literal[
    "recorded",
    "duplicate",
    "conflict",
    "payload_too_large",
    "capacity_reached",
]
_CanonicalValue: TypeAlias = str | int | float | bool | None | list["_CanonicalValue"] | dict[str, "_CanonicalValue"]


@dataclass(frozen=True)
class TomorrowResearchTraceWriteResult:
    status: TraceRecordStatus
    identity: str
    payload_bytes: int


@dataclass(frozen=True)
class TomorrowResearchTraceStoreStatus:
    retained_records: int
    retained_input_versions: int
    retained_bytes: int
    attempts: int
    recorded: int
    duplicate: int
    conflict: int
    payload_too_large: int
    capacity_reached: int


class InMemoryTomorrowResearchTraceStore:
    """Research-only bounded store; conflicting evidence never overwrites retained data."""

    def __init__(
        self,
        *,
        maximum_records: int = 2048,
        maximum_payload_bytes: int = 4 * 1024 * 1024,
        maximum_total_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        if min(maximum_records, maximum_payload_bytes, maximum_total_bytes) < 1:
            raise ValueError("research trace capacities must be positive")
        if maximum_payload_bytes > maximum_total_bytes:
            raise ValueError("research payload capacity cannot exceed total capacity")
        self._maximum_records = maximum_records
        self._maximum_payload_bytes = maximum_payload_bytes
        self._maximum_total_bytes = maximum_total_bytes
        self._lock = threading.Lock()
        self._records: dict[str, tuple[TomorrowResearchTrace, str, int]] = {}
        self._retained_bytes = 0
        self._attempts = 0
        self._recorded = 0
        self._duplicate = 0
        self._conflict = 0
        self._payload_too_large = 0
        self._capacity_reached = 0

    def record(self, trace: TomorrowResearchTrace) -> TomorrowResearchTraceWriteResult:
        payload = research_trace_payload(trace)
        return self.record_encoded(trace, payload, _payload_identity(payload))

    def record_encoded(
        self,
        trace: TomorrowResearchTrace,
        payload: bytes,
        identity: str,
    ) -> TomorrowResearchTraceWriteResult:
        expected_identity = _payload_identity(payload)
        if identity != expected_identity or payload != research_trace_payload(trace):
            raise ValueError("research trace encoded payload identity mismatch")
        payload_bytes = len(payload)
        key = trace.input_version
        with self._lock:
            self._attempts += 1
            if payload_bytes > self._maximum_payload_bytes:
                self._payload_too_large += 1
                return TomorrowResearchTraceWriteResult("payload_too_large", identity, payload_bytes)
            current = self._records.get(key)
            if current is not None:
                if current[1] == identity:
                    self._duplicate += 1
                    return TomorrowResearchTraceWriteResult("duplicate", identity, payload_bytes)
                self._conflict += 1
                return TomorrowResearchTraceWriteResult("conflict", identity, payload_bytes)
            if (
                len(self._records) >= self._maximum_records
                or self._retained_bytes + payload_bytes > self._maximum_total_bytes
            ):
                self._capacity_reached += 1
                return TomorrowResearchTraceWriteResult("capacity_reached", identity, payload_bytes)
            self._records[key] = (trace, identity, payload_bytes)
            self._retained_bytes += payload_bytes
            self._recorded += 1
            return TomorrowResearchTraceWriteResult("recorded", identity, payload_bytes)

    def get(self, input_version: str) -> TomorrowResearchTrace | None:
        with self._lock:
            retained = self._records.get(input_version)
            return retained[0] if retained is not None else None

    def status(self) -> TomorrowResearchTraceStoreStatus:
        with self._lock:
            return TomorrowResearchTraceStoreStatus(
                retained_records=len(self._records),
                retained_input_versions=len(self._records),
                retained_bytes=self._retained_bytes,
                attempts=self._attempts,
                recorded=self._recorded,
                duplicate=self._duplicate,
                conflict=self._conflict,
                payload_too_large=self._payload_too_large,
                capacity_reached=self._capacity_reached,
            )


class AsyncTomorrowResearchTraceRecorder:
    """Non-blocking adapter from the decision path to a lifecycle-owned bounded executor."""

    def __init__(self, store: InMemoryTomorrowResearchTraceStore, executor: WorkerExecutor) -> None:
        self._store = store
        self._executor = executor
        self._lock = threading.Lock()
        self._attempts = 0
        self._queued = 0
        self._queue_full = 0
        self._payload_too_large = 0
        self._completed = 0
        self._write_rejected = 0
        self._worker_failed = 0
        self._last_failure = ""

    def enqueue(self, capture: TomorrowResearchTraceCapture) -> TomorrowResearchTraceEnqueueResult:
        identity = _capture_identity(
            capture.projection.input_version,
            capture.baseline_snapshot_id,
        )
        with self._lock:
            self._attempts += 1
        future = self._executor.submit(self._build_and_record, capture)
        if future is None:
            with self._lock:
                self._queue_full += 1
                self._last_failure = "queue_full"
            return TomorrowResearchTraceEnqueueResult("queue_full", identity, 0)
        with self._lock:
            self._queued += 1
        future.add_done_callback(self._complete)
        return TomorrowResearchTraceEnqueueResult("queued", identity, 0)

    def status(self) -> TomorrowResearchTraceRecorderStatus:
        with self._lock:
            return TomorrowResearchTraceRecorderStatus(
                attempts=self._attempts,
                queued=self._queued,
                queue_full=self._queue_full,
                payload_too_large=self._payload_too_large,
                completed=self._completed,
                write_rejected=self._write_rejected,
                worker_failed=self._worker_failed,
                last_failure=self._last_failure,
            )

    def get(self, input_version: str) -> TomorrowResearchTrace | None:
        return self._store.get(input_version)

    def _complete(self, future: Future[TomorrowResearchTraceWriteResult]) -> None:
        try:
            result = future.result()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            with self._lock:
                self._worker_failed += 1
                self._last_failure = type(exc).__name__
            return
        with self._lock:
            self._completed += 1
            if result.status == "payload_too_large":
                self._payload_too_large += 1
            if result.status not in {"recorded", "duplicate"}:
                self._write_rejected += 1
                self._last_failure = result.status
            else:
                self._last_failure = ""

    def _build_and_record(
        self,
        capture: TomorrowResearchTraceCapture,
    ) -> TomorrowResearchTraceWriteResult:
        trace = build_tomorrow_research_trace(
            capture.projection,
            baseline_snapshot_id=capture.baseline_snapshot_id,
        )
        return self._store.record(trace)


def create_tomorrow_research_trace_recorder(executor: WorkerExecutor) -> AsyncTomorrowResearchTraceRecorder:
    """Compose the default bounded in-memory research recorder."""

    return AsyncTomorrowResearchTraceRecorder(InMemoryTomorrowResearchTraceStore(), executor)


def research_trace_payload(trace: TomorrowResearchTrace) -> bytes:
    canonical = _canonical_value(trace)
    return json.dumps(
        canonical,
        allow_nan=False,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _payload_identity(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _capture_identity(input_version: str, baseline_snapshot_id: str) -> str:
    payload = f"{input_version}|{baseline_snapshot_id}".encode()
    return _payload_identity(payload)


def _canonical_value(value: object) -> _CanonicalValue:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("research trace cannot contain non-finite values")
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return str(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        canonical: _CanonicalValue = {item.name: _canonical_value(getattr(value, item.name)) for item in fields(value)}
    elif isinstance(value, Mapping):
        canonical = {
            str(key): _canonical_value(item) for key, item in sorted(value.items(), key=lambda row: str(row[0]))
        }
    elif isinstance(value, (tuple, list)):
        canonical = [_canonical_value(item) for item in value]
    else:
        raise TypeError(f"unsupported research trace value: {type(value).__name__}")
    return canonical


__all__ = [
    "AsyncTomorrowResearchTraceRecorder",
    "InMemoryTomorrowResearchTraceStore",
    "TomorrowResearchTraceStoreStatus",
    "TomorrowResearchTraceWriteResult",
    "TraceRecordStatus",
    "create_tomorrow_research_trace_recorder",
    "research_trace_payload",
]
