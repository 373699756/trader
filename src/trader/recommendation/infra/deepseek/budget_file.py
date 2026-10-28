"""Single locked, durable recovery file; no network or budget policy here."""

import json
from datetime import datetime
from pathlib import Path

from trader.infra.atomic_files import AtomicWriteJson
from trader.infra.process_lock import ProcessLock
from trader.infra.serialization.canonical_codec import CanonicalDigest, CanonicalJsonText
from trader.recommendation.infra.deepseek.budget_codec import (
    MAX_STATE_BYTES,
    SCHEMA_VERSION,
    BudgetDay,
    DecodeBudgetState,
    EncodeBudgetState,
)
from trader.recommendation.infra.deepseek.budget_state import BudgetState


class BudgetStateFile:
    def __init__(self, path: Path, process_lock: ProcessLock) -> None:
        self._path = path.resolve()
        self._process_lock = process_lock
        self._identity = CanonicalDigest(str(self._path))
        self._signature: tuple[int, int, int, int] | None = None

    def open(self, at: datetime, policy_hash: str) -> BudgetState:
        self._process_lock.acquire()
        day = BudgetDay(at)
        try:
            if self._process_lock.read_identity() != self._identity:
                raise ValueError("budget_owner_location_changed")
            with self._path.open("rb") as handle:
                encoded = handle.read(MAX_STATE_BYTES + 1)
            if len(encoded) > MAX_STATE_BYTES:
                raise ValueError("budget_state_too_large")
            state = DecodeBudgetState(json.loads(encoded), self._identity)
            self._signature = self._file_signature()
            return state
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            # Unknown evidence never means zero usage. Persist the quarantine day
            # so a normal restart tomorrow can recover, without clearing today.
            state = BudgetState(
                SCHEMA_VERSION, day, policy_hash, 0, self._identity,
                blocked_date=day, blocked_reason="budget_state_unverified",
            )
            self.write(state, replace_unverified=True)
            self._process_lock.write_identity(self._identity)
            return state

    def write(self, state: BudgetState, *, replace_unverified: bool = False) -> None:
        if not replace_unverified and self._file_signature() != self._signature:
            raise OSError("budget_state_evidence_changed")
        payload = EncodeBudgetState(state)
        if len(CanonicalJsonText(payload, ensure_ascii=False).encode("utf-8")) > MAX_STATE_BYTES:
            raise OSError("budget_state_too_large")
        # Validate before writing too, so no successfully reserved request can
        # produce a recovery file that the next process would silently discard.
        DecodeBudgetState(payload, self._identity)
        AtomicWriteJson(self._path, payload)
        self._signature = self._file_signature()

    def close(self) -> None:
        self._process_lock.release()
        self._signature = None

    def _file_signature(self) -> tuple[int, int, int, int]:
        stat = self._path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


__all__ = ["BudgetStateFile"]
