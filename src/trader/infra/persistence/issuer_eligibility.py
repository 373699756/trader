"""Append-only SQLite registry for issuer-level permanent exclusions."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from trader.domain.market.eligibility import (
    IssuerEligibilityDecision,
    IssuerEligibilityFact,
    IssuerEligibilityReason,
    IssuerEligibilityReasonCount,
    IssuerEligibilityRegistryStatus,
    issuer_eligibility_fact_hash,
    manual_blacklist_fact,
    resolve_issuer_eligibility,
)

_SCHEMA_VERSION = "issuer_eligibility_registry"
_EMPTY_MANIFEST_HASH = hashlib.sha256(b"").hexdigest()


class IssuerEligibilityConflictError(RuntimeError):
    """The same immutable evidence identity was observed with different content."""


class SQLiteIssuerEligibilityRegistry:
    def __init__(self, database_path: Path, *, read_only: bool = False) -> None:
        self._database_path = database_path
        self._read_only = read_only
        self._lock = threading.RLock()
        self._loaded = False
        self._facts: dict[tuple[str, IssuerEligibilityReason, str], IssuerEligibilityFact] = {}
        self._integrity_ok = True
        self._persistence_error_count = 0
        self._last_error: str | None = None

    def record(self, facts: Sequence[IssuerEligibilityFact]) -> int:
        incoming = tuple(sorted(set(facts)))
        if not incoming:
            return 0
        if self._read_only:
            raise RuntimeError("read-only issuer eligibility registry cannot record facts")
        with self._lock:
            self._ensure_loaded()
            self._validate_incoming(incoming)
            try:
                self._database_path.parent.mkdir(parents=True, exist_ok=True)
                with self._connection() as connection:
                    self._ensure_schema(connection)
                    inserted = 0
                    for fact in incoming:
                        identity_key = _identity_key(fact)
                        content_hash = issuer_eligibility_fact_hash(fact)
                        existing = connection.execute(
                            "SELECT content_hash FROM issuer_eligibility_facts WHERE identity_key = ?",
                            (identity_key,),
                        ).fetchone()
                        if existing is not None:
                            if str(existing[0]) != content_hash:
                                raise IssuerEligibilityConflictError(
                                    f"issuer eligibility evidence conflict: {fact.code}:{fact.reason.value}"
                                )
                            continue
                        connection.execute(
                            """
                            INSERT INTO issuer_eligibility_facts (
                                identity_key, code, reason, effective_at, evidence_id, source,
                                evidence_hash, content_hash
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                identity_key,
                                fact.code,
                                fact.reason.value,
                                fact.effective_at.isoformat(),
                                fact.evidence_id,
                                fact.source,
                                fact.evidence_hash,
                                content_hash,
                            ),
                        )
                        inserted += 1
                    connection.commit()
            except IssuerEligibilityConflictError:
                raise
            except (OSError, sqlite3.Error) as exc:
                self._persistence_error_count += 1
                self._last_error = "eligibility_persistence_error"
                raise RuntimeError("issuer eligibility persistence failed") from exc
            for fact in incoming:
                self._facts.setdefault(fact.identity, fact)
            self._last_error = None
            return inserted

    def record_manual_blacklist(
        self,
        codes: Sequence[str],
        effective_at: datetime,
        config_hash: str,
    ) -> int:
        existing = {
            fact.code
            for fact in self.facts()
            if fact.reason is IssuerEligibilityReason.MANUAL_PERMANENT_BLACKLIST and fact.source == "strategy_config"
        }
        return self.record(
            tuple(
                manual_blacklist_fact(code, effective_at, config_hash)
                for code in tuple(dict.fromkeys(codes))
                if code not in existing
            )
        )

    def filter_codes(self, codes: Sequence[str], observed_at: datetime) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(code.strip() for code in codes if code.strip()))
        with self._lock:
            self._ensure_loaded()
            active_codes = frozenset(_active_facts_by_code(tuple(self._facts.values()), observed_at))
        return tuple(code for code in normalized if code not in active_codes)

    def exclusions(self, observed_at: datetime) -> tuple[IssuerEligibilityDecision, ...]:
        with self._lock:
            self._ensure_loaded()
            facts = tuple(self._facts.values())
        return tuple(
            resolve_issuer_eligibility((fact,), code, observed_at)
            for code, fact in sorted(_active_facts_by_code(facts, observed_at).items())
        )

    def facts(self) -> tuple[IssuerEligibilityFact, ...]:
        with self._lock:
            self._ensure_loaded()
            return tuple(sorted(self._facts.values()))

    def status(self) -> IssuerEligibilityRegistryStatus:
        facts = self.facts()
        reason_counts = Counter(fact.reason for fact in facts)
        manifest = _manifest_hash(facts)
        with self._lock:
            return IssuerEligibilityRegistryStatus(
                schema_version=_SCHEMA_VERSION,
                fact_count=len(facts),
                excluded_count=len({fact.code for fact in facts}),
                reason_counts=tuple(
                    IssuerEligibilityReasonCount(reason, reason_counts[reason])
                    for reason in sorted(reason_counts, key=lambda item: item.value)
                ),
                manifest_hash=manifest,
                integrity_ok=self._integrity_ok,
                persistence_error_count=self._persistence_error_count,
                last_error=self._last_error,
            )

    def _validate_incoming(self, facts: tuple[IssuerEligibilityFact, ...]) -> None:
        for fact in facts:
            existing = self._facts.get(fact.identity)
            if existing is not None and issuer_eligibility_fact_hash(existing) != issuer_eligibility_fact_hash(fact):
                raise IssuerEligibilityConflictError(
                    f"issuer eligibility evidence conflict: {fact.code}:{fact.reason.value}"
                )

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._database_path.exists():
            return
        try:
            with self._connection(read_only=True) as connection:
                rows = connection.execute(
                    """
                    SELECT code, reason, effective_at, evidence_id, source, evidence_hash, content_hash
                    FROM issuer_eligibility_facts
                    ORDER BY code, reason, effective_at, evidence_id
                    """
                ).fetchall()
        except (OSError, sqlite3.Error):
            self._integrity_ok = False
            self._last_error = "eligibility_integrity_error"
            return
        for row in rows:
            try:
                fact = IssuerEligibilityFact(
                    code=str(row[0]),
                    reason=IssuerEligibilityReason(str(row[1])),
                    effective_at=datetime.fromisoformat(str(row[2])),
                    evidence_id=str(row[3]),
                    source=str(row[4]),
                    evidence_hash=str(row[5]),
                )
            except (TypeError, ValueError):
                self._integrity_ok = False
                self._last_error = "eligibility_integrity_error"
                continue
            if issuer_eligibility_fact_hash(fact) != str(row[6]):
                self._integrity_ok = False
                self._last_error = "eligibility_integrity_error"
            self._facts[fact.identity] = fact

    def _connection(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            return sqlite3.connect(f"file:{self._database_path}?mode=ro", uri=True, timeout=5.0)
        return sqlite3.connect(self._database_path, timeout=5.0)

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS issuer_eligibility_facts (
                identity_key TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                reason TEXT NOT NULL,
                effective_at TEXT NOT NULL,
                evidence_id TEXT NOT NULL,
                source TEXT NOT NULL,
                evidence_hash TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                UNIQUE(code, reason, evidence_id)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_issuer_eligibility_effective ON issuer_eligibility_facts(code, effective_at)"
        )


def _identity_key(fact: IssuerEligibilityFact) -> str:
    payload = "\x1f".join((fact.code, fact.reason.value, fact.evidence_id)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _manifest_hash(facts: tuple[IssuerEligibilityFact, ...]) -> str:
    if not facts:
        return _EMPTY_MANIFEST_HASH
    payload = "\n".join(issuer_eligibility_fact_hash(fact) for fact in facts).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _active_facts_by_code(
    facts: tuple[IssuerEligibilityFact, ...],
    observed_at: datetime,
) -> dict[str, IssuerEligibilityFact]:
    active: dict[str, IssuerEligibilityFact] = {}
    for fact in facts:
        if fact.effective_at > observed_at:
            continue
        current = active.get(fact.code)
        if current is None or fact < current:
            active[fact.code] = fact
    return active


__all__ = [
    "IssuerEligibilityConflictError",
    "SQLiteIssuerEligibilityRegistry",
]
