"""Immutable in-process freeze attempts and deterministic retry metadata."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum

_RETRY_DELAYS_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0)


@dataclass(frozen=True, order=True)
class FreezeAttemptKey:
    strategy: str
    trade_date: str

    def __post_init__(self) -> None:
        if not self.strategy or not self.trade_date:
            raise ValueError("freeze attempt identity cannot be empty")
        date.fromisoformat(self.trade_date)


@dataclass(frozen=True)
class FreezeAttempt:
    strategy: str
    trade_date: str
    boundary_at: datetime
    frozen_snapshot: object | None
    frozen_decision: object | None
    canonical_payload: bytes
    canonical_sha256: str
    attempt_count: int = 0
    next_retry_at: datetime | None = None

    def __post_init__(self) -> None:
        if (self.frozen_snapshot is None) == (self.frozen_decision is None):
            raise ValueError("freeze attempt requires exactly one frozen object")
        if self.boundary_at.tzinfo is None or self.boundary_at.utcoffset() is None:
            raise ValueError("freeze boundary must be timezone-aware")
        if self.attempt_count < 0:
            raise ValueError("freeze attempt count cannot be negative")
        if self.next_retry_at is not None and (
            self.next_retry_at.tzinfo is None or self.next_retry_at.utcoffset() is None
        ):
            raise ValueError("freeze retry time must be timezone-aware")
        if hashlib.sha256(self.canonical_payload).hexdigest() != self.canonical_sha256:
            raise ValueError("freeze attempt canonical SHA-256 does not match its payload")
        if self.strategy != self.key.strategy or self.trade_date != self.key.trade_date:
            raise ValueError("freeze attempt identity is inconsistent")

    @property
    def key(self) -> FreezeAttemptKey:
        return FreezeAttemptKey(self.strategy, self.trade_date)

    @property
    def snapshot_id(self) -> str | None:
        value = getattr(self.frozen_snapshot, "snapshot_id", None)
        return value if isinstance(value, str) else None

    @property
    def decision_id(self) -> str | None:
        value = getattr(self.frozen_decision, "version", None)
        return value if isinstance(value, str) else None


class FreezeAttemptStore:
    """Keep one sealed object per strategy and trade date for the process lifetime."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._attempts: dict[FreezeAttemptKey, FreezeAttempt] = {}
        self._completed: set[FreezeAttemptKey] = set()
        self._missed: set[FreezeAttemptKey] = set()

    def seal_snapshot(
        self,
        *,
        strategy: str,
        trade_date: str,
        boundary_at: datetime,
        frozen_snapshot: object,
    ) -> FreezeAttempt:
        return self._seal(
            FreezeAttempt(
                strategy=strategy,
                trade_date=trade_date,
                boundary_at=boundary_at,
                frozen_snapshot=frozen_snapshot,
                frozen_decision=None,
                canonical_payload=canonical_bytes(frozen_snapshot),
                canonical_sha256=canonical_sha256(frozen_snapshot),
            )
        )

    def seal_decision(
        self,
        *,
        strategy: str,
        trade_date: str,
        boundary_at: datetime,
        frozen_decision: object,
    ) -> FreezeAttempt:
        return self._seal(
            FreezeAttempt(
                strategy=strategy,
                trade_date=trade_date,
                boundary_at=boundary_at,
                frozen_snapshot=None,
                frozen_decision=frozen_decision,
                canonical_payload=canonical_bytes(frozen_decision),
                canonical_sha256=canonical_sha256(frozen_decision),
            )
        )

    def _seal(self, candidate: FreezeAttempt) -> FreezeAttempt:
        with self._lock:
            current = self._attempts.get(candidate.key)
            if current is None:
                self._attempts[candidate.key] = candidate
                return candidate
            if (
                current.canonical_sha256 != candidate.canonical_sha256
                or current.canonical_payload != candidate.canonical_payload
            ):
                raise ValueError("a different freeze object already owns this strategy and trade date")
            return current

    def get(self, key: FreezeAttemptKey) -> FreezeAttempt | None:
        with self._lock:
            return self._attempts.get(key)

    def retry(self, key: FreezeAttemptKey, *, at: datetime) -> FreezeAttempt:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("freeze retry clock must be timezone-aware")
        with self._lock:
            current = self._attempts[key]
            attempt_count = current.attempt_count + 1
            delay_index = min(attempt_count - 1, len(_RETRY_DELAYS_SECONDS) - 1)
            updated = replace(
                current,
                attempt_count=attempt_count,
                next_retry_at=at + timedelta(seconds=_RETRY_DELAYS_SECONDS[delay_index]),
            )
            self._attempts[key] = updated
            return updated

    def mark_completed(self, key: FreezeAttemptKey) -> None:
        with self._lock:
            if key in self._attempts:
                self._completed.add(key)
                self._missed.discard(key)

    def mark_missed(self, key: FreezeAttemptKey) -> None:
        with self._lock:
            self._missed.add(key)

    def active(self, *, trade_date: str | None = None) -> tuple[FreezeAttempt, ...]:
        with self._lock:
            return tuple(
                attempt
                for key, attempt in sorted(self._attempts.items())
                if key not in self._completed
                and key not in self._missed
                and (trade_date is None or key.trade_date == trade_date)
            )

    def status(self) -> Mapping[str, object]:
        with self._lock:
            return {
                "attempts": len(self._attempts),
                "active": sum(key not in self._completed and key not in self._missed for key in self._attempts),
                "completed": len(self._completed),
                "missed": len(self._missed),
                "retry_wait": sum(
                    attempt.next_retry_at is not None and key not in self._completed and key not in self._missed
                    for key, attempt in self._attempts.items()
                ),
            }


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _canonical_value(value: object) -> object:
    result: object
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        result = {item.name: _canonical_value(getattr(value, item.name)) for item in dataclasses.fields(value)}
    elif isinstance(value, Enum):
        result = _canonical_value(value.value)
    elif isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("canonical freeze times must be timezone-aware")
        result = value.isoformat()
    elif isinstance(value, date):
        result = value.isoformat()
    else:
        result = _canonical_container_or_scalar(value)
    return result


def _canonical_container_or_scalar(value: object) -> object:
    if isinstance(value, Mapping):
        result: object = {
            str(key): _canonical_value(item) for key, item in sorted(value.items(), key=lambda row: str(row[0]))
        }
    elif isinstance(value, (tuple, list)):
        result = [_canonical_value(item) for item in value]
    elif isinstance(value, (set, frozenset)):
        result = sorted((_canonical_value(item) for item in value), key=_canonical_sort_key)
    elif isinstance(value, Decimal):
        result = str(value)
    elif isinstance(value, bytes):
        result = value.hex()
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical freeze numbers must be finite")
        result = value
    elif value is None or isinstance(value, (str, int, bool)):
        result = value
    else:
        raise TypeError(f"unsupported canonical freeze value: {type(value).__name__}")
    return result


def _canonical_sort_key(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "FreezeAttempt",
    "FreezeAttemptKey",
    "FreezeAttemptStore",
    "canonical_bytes",
    "canonical_sha256",
]
