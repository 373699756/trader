"""Atomic SQLite facts and publication identity for permanent issuer exclusions."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections import Counter
from collections.abc import Sequence
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from trader.infra.sqlite_connection import (
    EnsureSqliteSchema,
    OpenSqliteConnection,
    SqliteConnection,
    SqliteConnectionOptions,
    SqliteSchema,
    SqliteTransaction,
)
from trader.recommendation.domain.market.eligibility import (
    IsActivePermanentEligibilityReason,
    IssuerEligibilityDecision,
    IssuerEligibilityFact,
    IssuerEligibilityFactHash,
    IssuerEligibilityReason,
    IssuerEligibilityReasonCount,
    IssuerEligibilitySnapshot,
    ManualBlacklistFact,
    ResolveIssuerEligibility,
)

_SCHEMA_VERSION = "issuer_eligibility_snapshot"
_EMPTY_MANIFEST_HASH = hashlib.sha256(b"").hexdigest()


class IssuerEligibilityConflictError(RuntimeError):
    """The same immutable evidence identity was observed with different content."""


class SQLiteIssuerEligibilityIndex:
    """Single owner for facts and their transactionally published integrity identity."""

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self._path = path
        self._read_only = read_only
        self._lock = threading.RLock()
        self._facts: dict[tuple[str, IssuerEligibilityReason, str], IssuerEligibilityFact] = {}
        self._publication_hash: str | None = None
        self._integrity_ok = True
        self._persistence_error_count = 0
        self._last_error: str | None = None
        self._refresh_state = "not_ready"
        self._last_refresh_at: datetime | None = None
        self._next_refresh_at: datetime | None = None
        self._refresh_failure_count = 0

    def initialize(self) -> None:
        if self._read_only:
            raise RuntimeError("read-only issuer eligibility index cannot initialize")
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with closing(OpenSqliteConnection(self._path, SqliteConnectionOptions(journal_mode="WAL"))) as connection:
                EnsureSqliteSchema(connection, _SCHEMA, allow_create=True)
                with SqliteTransaction(connection, immediate=True):
                    facts = self._checked_facts(connection)
                    connection.run(
                        "INSERT OR IGNORE INTO publication(singleton,content_hash,fact_count) VALUES (1,?,?)",
                        (_manifest_hash(tuple(facts.values())), len(facts)),
                    )
            self._ensure_loaded()

    def record(self, facts: Sequence[IssuerEligibilityFact]) -> int:
        incoming = tuple(sorted(set(facts)))
        if not incoming:
            return 0
        if self._read_only:
            raise RuntimeError("read-only issuer eligibility index cannot record facts")
        with self._lock:
            self._ensure_loaded()
            self._validate_incoming(incoming)
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with closing(
                    OpenSqliteConnection(self._path, SqliteConnectionOptions(journal_mode="WAL"))
                ) as connection:
                    EnsureSqliteSchema(connection, _SCHEMA, allow_create=True)
                    with SqliteTransaction(connection, immediate=True):
                        current = self._checked_facts(connection)
                        inserted = 0
                        for fact in incoming:
                            previous = current.get(fact.identity)
                            if previous is not None:
                                if previous != fact:
                                    raise IssuerEligibilityConflictError("issuer eligibility evidence conflict")
                                continue
                            connection.run(
                                "INSERT INTO issuer_eligibility_facts VALUES (?,?,?,?,?,?,?,?)",
                                (
                                    _identity_key(fact),
                                    fact.code,
                                    fact.reason.value,
                                    fact.effective_at.isoformat(),
                                    fact.evidence_id,
                                    fact.source,
                                    fact.evidence_hash,
                                    IssuerEligibilityFactHash(fact),
                                ),
                            )
                            current[fact.identity] = fact
                            inserted += 1
                        digest = _manifest_hash(tuple(current.values()))
                        connection.run(
                            "INSERT INTO publication(singleton,content_hash,fact_count) VALUES (1,?,?) "
                            "ON CONFLICT(singleton) DO UPDATE SET content_hash=excluded.content_hash,fact_count=excluded.fact_count",
                            (digest, len(current)),
                        )
                    self._facts = current
                    self._publication_hash = digest
            except IssuerEligibilityConflictError:
                raise
            except (OSError, sqlite3.Error) as exc:
                self._persistence_error_count += 1
                self._last_error = "eligibility_persistence_error"
                raise RuntimeError("issuer eligibility persistence failed") from exc
            self._last_error = None
            return inserted

    def refresh_due(self, observed_at: datetime) -> bool:
        if self._read_only:
            return False
        with self._lock:
            self._ensure_loaded()
            if self._next_refresh_at is None:
                self._next_refresh_at = _next_friday_refresh(observed_at - timedelta(microseconds=1))
            return observed_at >= self._next_refresh_at

    def refresh_snapshot(self, observed_at: datetime, *, source: str) -> None:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("issuer eligibility refresh time must be timezone-aware")
        with self._lock:
            self._ensure_loaded()
            self._last_refresh_at = observed_at
            self._next_refresh_at = _next_friday_refresh(observed_at)
            self._refresh_state = "ready"
            self._last_error = None
            self._write_manifest(source=source)

    def refresh_failed(self, observed_at: datetime, reason: str) -> None:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("issuer eligibility refresh time must be timezone-aware")
        with self._lock:
            self._ensure_loaded()
            self._refresh_failure_count += 1
            self._refresh_state = "failed"
            self._last_error = reason
            self._next_refresh_at = observed_at + timedelta(hours=1)
            self._write_manifest(source="refresh_failure")

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
                ManualBlacklistFact(code, effective_at, config_hash)
                for code in tuple(dict.fromkeys(codes))
                if code not in existing
            )
        )

    def filter_codes(self, codes: Sequence[str], observed_at: datetime) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(code.strip() for code in codes if code.strip()))
        with self._lock:
            self._ensure_loaded()
            if not self._integrity_ok and self._publication_hash is None:
                return ()
            active_codes = frozenset(_active_facts_by_code(tuple(self._facts.values()), observed_at))
        return tuple(code for code in normalized if code not in active_codes)

    def exclusions(self, observed_at: datetime) -> tuple[IssuerEligibilityDecision, ...]:
        with self._lock:
            self._ensure_loaded()
            facts = tuple(self._facts.values())
        return tuple(
            ResolveIssuerEligibility((fact,), code, observed_at)
            for code, fact in sorted(_active_facts_by_code(facts, observed_at).items())
        )

    def facts(self) -> tuple[IssuerEligibilityFact, ...]:
        with self._lock:
            self._ensure_loaded()
            return tuple(sorted(self._facts.values()))

    def status(self) -> IssuerEligibilitySnapshot:
        facts = self.facts()
        active_facts = tuple(fact for fact in facts if IsActivePermanentEligibilityReason(fact.reason))
        reason_counts = Counter(fact.reason for fact in active_facts)
        manifest = _manifest_hash(facts)
        with self._lock:
            return IssuerEligibilitySnapshot(
                schema_version=_SCHEMA_VERSION,
                fact_count=len(active_facts),
                excluded_count=len({fact.code for fact in active_facts}),
                reason_counts=tuple(
                    IssuerEligibilityReasonCount(reason, reason_counts[reason])
                    for reason in sorted(reason_counts, key=lambda item: item.value)
                ),
                manifest_hash=manifest,
                integrity_ok=self._integrity_ok,
                persistence_error_count=self._persistence_error_count,
                last_error=self._last_error,
                refresh_state=self._refresh_state,
                last_refresh_at=self._last_refresh_at,
                next_refresh_at=self._next_refresh_at,
                refresh_failure_count=self._refresh_failure_count,
            )

    def _validate_incoming(self, facts: tuple[IssuerEligibilityFact, ...]) -> None:
        for fact in facts:
            existing = self._facts.get(fact.identity)
            if existing is not None and IssuerEligibilityFactHash(existing) != IssuerEligibilityFactHash(fact):
                raise IssuerEligibilityConflictError(
                    f"issuer eligibility evidence conflict: {fact.code}:{fact.reason.value}"
                )

    def _ensure_loaded(self) -> None:
        if not self._path.exists():
            return
        try:
            with closing(OpenSqliteConnection(self._path, SqliteConnectionOptions(read_only=True))) as connection:
                EnsureSqliteSchema(connection, _SCHEMA)
                with SqliteTransaction(connection):
                    metadata = connection.run(
                        "SELECT content_hash,fact_count,refresh_state,last_refresh_at,next_refresh_at,refresh_failure_count,last_error "
                        "FROM publication WHERE singleton=1"
                    ).fetchone()
                    if metadata is None:
                        raise ValueError("eligibility_publication_missing")
                    if metadata[0] != self._publication_hash:
                        current = self._read_facts(connection)
                        if metadata[0] != _manifest_hash(tuple(current.values())) or metadata[1] != len(current):
                            raise ValueError("eligibility_publication_invalid")
                        self._facts = current
                        self._publication_hash = str(metadata[0])
                    self._refresh_state = str(metadata[2])
                    self._last_refresh_at = _optional_datetime(metadata[3])
                    self._next_refresh_at = _optional_datetime(metadata[4])
                    self._refresh_failure_count = int(metadata[5])
                    self._last_error = metadata[6]
                    self._integrity_ok = True
        except (OSError, ValueError, TypeError, sqlite3.Error):
            self._integrity_ok = False
            self._last_error = "eligibility_integrity_error"

    @staticmethod
    def _checked_facts(
        connection: SqliteConnection,
    ) -> dict[tuple[str, IssuerEligibilityReason, str], IssuerEligibilityFact]:
        facts = SQLiteIssuerEligibilityIndex._read_facts(connection)
        metadata = connection.run("SELECT content_hash,fact_count FROM publication WHERE singleton=1").fetchone()
        if metadata is None:
            if facts:
                raise ValueError("eligibility_publication_missing")
        elif tuple(metadata) != (_manifest_hash(tuple(facts.values())), len(facts)):
            raise ValueError("eligibility_publication_invalid")
        return facts

    @staticmethod
    def _read_facts(
        connection: SqliteConnection,
    ) -> dict[tuple[str, IssuerEligibilityReason, str], IssuerEligibilityFact]:
        facts: dict[tuple[str, IssuerEligibilityReason, str], IssuerEligibilityFact] = {}
        for row in connection.run(
            "SELECT code,reason,effective_at,evidence_id,source,evidence_hash,content_hash "
            "FROM issuer_eligibility_facts ORDER BY code,reason,effective_at,evidence_id"
        ):
            fact = IssuerEligibilityFact(
                str(row[0]),
                IssuerEligibilityReason(str(row[1])),
                datetime.fromisoformat(str(row[2])),
                str(row[3]),
                str(row[4]),
                str(row[5]),
            )
            if IssuerEligibilityFactHash(fact) != row[6]:
                raise ValueError("eligibility_fact_invalid")
            facts[fact.identity] = fact
        return facts

    def _write_manifest(self, *, source: str = "incremental") -> None:
        if self._read_only:
            raise RuntimeError("read-only issuer eligibility index cannot publish")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(OpenSqliteConnection(self._path, SqliteConnectionOptions(journal_mode="WAL"))) as connection:
                EnsureSqliteSchema(connection, _SCHEMA, allow_create=True)
                with SqliteTransaction(connection, immediate=True):
                    facts = self._checked_facts(connection)
                    previous = connection.run("SELECT last_refresh_at FROM publication WHERE singleton=1").fetchone()
                    if (
                        previous is not None
                        and previous[0] is not None
                        and self._last_refresh_at is not None
                        and datetime.fromisoformat(previous[0]) > self._last_refresh_at
                    ):
                        raise ValueError("eligibility_refresh_would_regress")
                    digest = _manifest_hash(tuple(facts.values()))
                    connection.run(
                        "INSERT INTO publication VALUES (1,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(singleton) DO UPDATE SET content_hash=excluded.content_hash,"
                        "fact_count=excluded.fact_count,refresh_state=excluded.refresh_state,"
                        "last_refresh_at=excluded.last_refresh_at,next_refresh_at=excluded.next_refresh_at,"
                        "refresh_failure_count=excluded.refresh_failure_count,last_error=excluded.last_error,last_source=excluded.last_source",
                        (
                            digest,
                            len(facts),
                            self._refresh_state,
                            self._last_refresh_at.isoformat() if self._last_refresh_at else None,
                            self._next_refresh_at.isoformat() if self._next_refresh_at else None,
                            self._refresh_failure_count,
                            self._last_error,
                            source,
                        ),
                    )
                self._facts = facts
                self._publication_hash = digest
        except (OSError, ValueError, sqlite3.Error) as exc:
            self._publication_hash = None
            self._ensure_loaded()
            self._persistence_error_count += 1
            self._last_error = "eligibility_persistence_error"
            raise RuntimeError("issuer eligibility publication failed") from exc


_SCHEMA = SqliteSchema(
    1,
    (
        "CREATE TABLE issuer_eligibility_facts (identity_key TEXT PRIMARY KEY,code TEXT NOT NULL,"
        "reason TEXT NOT NULL,effective_at TEXT NOT NULL,evidence_id TEXT NOT NULL,source TEXT NOT NULL,"
        "evidence_hash TEXT NOT NULL,content_hash TEXT NOT NULL,UNIQUE(code,reason,evidence_id))",
        "CREATE INDEX idx_issuer_eligibility_effective ON issuer_eligibility_facts(code,effective_at)",
        "CREATE TABLE publication(singleton INTEGER PRIMARY KEY CHECK(singleton=1),content_hash TEXT NOT NULL,"
        "fact_count INTEGER NOT NULL,refresh_state TEXT NOT NULL DEFAULT 'not_ready',last_refresh_at TEXT,"
        "next_refresh_at TEXT,refresh_failure_count INTEGER NOT NULL DEFAULT 0,last_error TEXT,last_source TEXT NOT NULL DEFAULT 'incremental')",
    ),
)


def _identity_key(fact: IssuerEligibilityFact) -> str:
    payload = "\x1f".join((fact.code, fact.reason.value, fact.evidence_id)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _manifest_hash(facts: tuple[IssuerEligibilityFact, ...]) -> str:
    if not facts:
        return _EMPTY_MANIFEST_HASH
    payload = "\n".join(IssuerEligibilityFactHash(fact) for fact in sorted(facts)).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _active_facts_by_code(
    facts: tuple[IssuerEligibilityFact, ...],
    observed_at: datetime,
) -> dict[str, IssuerEligibilityFact]:
    active: dict[str, IssuerEligibilityFact] = {}
    for fact in facts:
        if fact.effective_at > observed_at or not IsActivePermanentEligibilityReason(fact.reason):
            continue
        current = active.get(fact.code)
        if current is None or fact < current:
            active[fact.code] = fact
    return active


def _next_friday_refresh(value: datetime) -> datetime:
    candidate = value.replace(hour=15, minute=0, second=0, microsecond=0)
    days = (4 - value.weekday()) % 7
    if days == 0 and value >= candidate:
        days = 7
    return candidate + timedelta(days=days)


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("issuer eligibility refresh timestamp is invalid")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("issuer eligibility refresh timestamp must be timezone-aware")
    return parsed


__all__ = [
    "IssuerEligibilityConflictError",
    "SQLiteIssuerEligibilityIndex",
]
