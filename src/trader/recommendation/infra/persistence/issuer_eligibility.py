"""Categorized SQLite snapshot registry for issuer-level permanent exclusions."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

from trader.recommendation.domain.market.eligibility import (
    IssuerEligibilityDecision,
    IssuerEligibilityFact,
    IssuerEligibilityReason,
    IssuerEligibilityReasonCount,
    IssuerEligibilityRegistryStatus,
    issuer_eligibility_fact_hash,
    manual_blacklist_fact,
    resolve_issuer_eligibility,
)

_SCHEMA_VERSION = "issuer_eligibility_snapshot"
_EMPTY_MANIFEST_HASH = hashlib.sha256(b"").hexdigest()
_ACTIVE_MANIFEST = "active-manifest.json"
_SNAPSHOT_ROOT = "snapshots"
_CATEGORY_BY_REASON = {
    IssuerEligibilityReason.HISTORICAL_ST: "st_warning",
    IssuerEligibilityReason.HISTORICAL_DELISTING_WARNING: "delisting_warning",
    IssuerEligibilityReason.HISTORICAL_AUDITED_LOSS: "audited_loss",
    IssuerEligibilityReason.CONFIRMED_FINANCIAL_FRAUD: "financial_fraud",
    IssuerEligibilityReason.CONFIRMED_MAJOR_ILLEGAL: "major_illegal",
    IssuerEligibilityReason.CONFIRMED_FUND_OCCUPATION: "fund_occupation",
    IssuerEligibilityReason.CONFIRMED_ILLEGAL_GUARANTEE: "illegal_guarantee",
    IssuerEligibilityReason.CONFIRMED_FORCED_DELISTING: "forced_delisting",
    IssuerEligibilityReason.MANUAL_PERMANENT_BLACKLIST: "manual",
}


class IssuerEligibilityConflictError(RuntimeError):
    """The same immutable evidence identity was observed with different content."""


class SQLiteIssuerEligibilityRegistry:
    """Single owner for the categorized active eligibility snapshot."""

    def __init__(self, root: Path, *, read_only: bool = False) -> None:
        self._root = root
        self._manifest_path = root / _ACTIVE_MANIFEST
        self._read_only = read_only
        self._lock = threading.RLock()
        self._loaded = False
        self._facts: dict[tuple[str, IssuerEligibilityReason, str], IssuerEligibilityFact] = {}
        self._snapshot_id: str | None = None
        self._snapshot_dir: Path | None = None
        self._integrity_ok = True
        self._persistence_error_count = 0
        self._last_error: str | None = None
        self._refresh_state = "not_ready"
        self._last_refresh_at: datetime | None = None
        self._next_refresh_at: datetime | None = None
        self._refresh_failure_count = 0

    @classmethod
    def migrate_legacy_database(cls, database_path: Path, root: Path) -> int:
        """Import the retired single-file registry once, without retaining a read path."""
        if (root / _ACTIVE_MANIFEST).exists() or not database_path.exists():
            return 0
        try:
            with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=5.0) as connection:
                rows = connection.execute(
                    """
                    SELECT code, reason, effective_at, evidence_id, source, evidence_hash, content_hash
                    FROM issuer_eligibility_facts
                    ORDER BY code, reason, effective_at, evidence_id
                    """
                ).fetchall()
        except (OSError, sqlite3.Error) as exc:
            raise RuntimeError("legacy issuer eligibility migration failed") from exc
        facts: list[IssuerEligibilityFact] = []
        for row in rows:
            fact = IssuerEligibilityFact(
                code=str(row[0]),
                reason=IssuerEligibilityReason(str(row[1])),
                effective_at=datetime.fromisoformat(str(row[2])),
                evidence_id=str(row[3]),
                source=str(row[4]),
                evidence_hash=str(row[5]),
            )
            if issuer_eligibility_fact_hash(fact) != str(row[6]):
                raise RuntimeError("legacy issuer eligibility migration found invalid content")
            facts.append(fact)
        return cls(root).record(tuple(facts))

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
                snapshot_dir = self._ensure_snapshot()
                inserted = self._write_facts(snapshot_dir, incoming)
                if inserted:
                    for fact in incoming:
                        self._facts.setdefault(fact.identity, fact)
                    self._write_manifest()
            except IssuerEligibilityConflictError:
                raise
            except (OSError, sqlite3.Error) as exc:
                self._persistence_error_count += 1
                self._last_error = "eligibility_persistence_error"
                raise RuntimeError("issuer eligibility persistence failed") from exc
            self._last_error = None
            return inserted

    def refresh_due(self, observed_at: datetime) -> bool:
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
            self._ensure_snapshot()
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
            self._ensure_snapshot()
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
                refresh_state=self._refresh_state,
                last_refresh_at=self._last_refresh_at,
                next_refresh_at=self._next_refresh_at,
                refresh_failure_count=self._refresh_failure_count,
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
        if not self._manifest_path.exists():
            return
        try:
            manifest = json.loads(self._manifest_path.read_text(encoding="utf-8"))
            snapshot_id = str(manifest["snapshot_id"])
            snapshot_dir = self._root / _SNAPSHOT_ROOT / snapshot_id
            if not snapshot_dir.is_dir():
                raise ValueError("active eligibility snapshot directory is missing")
            self._snapshot_id = snapshot_id
            self._snapshot_dir = snapshot_dir
            self._refresh_state = str(manifest.get("refresh_state", "not_ready"))
            self._last_refresh_at = _optional_datetime(manifest.get("last_refresh_at"))
            self._next_refresh_at = _optional_datetime(manifest.get("next_refresh_at"))
            self._refresh_failure_count = int(manifest.get("refresh_failure_count", 0))
            for category in _CATEGORY_BY_REASON.values():
                database_path = snapshot_dir / f"{category}.sqlite3"
                if not database_path.exists():
                    continue
                self._load_database(database_path)
            if manifest.get("schema_version") != _SCHEMA_VERSION:
                raise ValueError("active eligibility snapshot schema is unsupported")
            if int(manifest.get("fact_count", -1)) != len(self._facts):
                raise ValueError("active eligibility snapshot fact count is invalid")
            if str(manifest.get("manifest_hash")) != _manifest_hash(tuple(self._facts.values())):
                raise ValueError("active eligibility snapshot manifest is invalid")
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, sqlite3.Error):
            self._integrity_ok = False
            self._last_error = "eligibility_integrity_error"
            return

    def _load_database(self, database_path: Path) -> None:
        with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=5.0) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("eligibility database integrity check failed")
            rows = connection.execute(
                """
                SELECT code, reason, effective_at, evidence_id, source, evidence_hash, content_hash
                FROM issuer_eligibility_facts
                ORDER BY code, reason, effective_at, evidence_id
                """
            ).fetchall()
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

    def _ensure_snapshot(self) -> Path:
        if self._snapshot_dir is not None:
            return self._snapshot_dir
        self._root.mkdir(parents=True, exist_ok=True)
        snapshot_id = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        snapshot_dir = self._root / _SNAPSHOT_ROOT / snapshot_id
        snapshot_dir.mkdir(parents=True, exist_ok=False)
        self._snapshot_id = snapshot_id
        self._snapshot_dir = snapshot_dir
        return snapshot_dir

    def _write_facts(self, snapshot_dir: Path, facts: tuple[IssuerEligibilityFact, ...]) -> int:
        grouped: dict[str, list[IssuerEligibilityFact]] = {}
        for fact in facts:
            grouped.setdefault(_CATEGORY_BY_REASON[fact.reason], []).append(fact)
        inserted = 0
        for category, category_facts in grouped.items():
            database_path = snapshot_dir / f"{category}.sqlite3"
            with sqlite3.connect(database_path, timeout=5.0) as connection:
                self._ensure_schema(connection)
                for fact in category_facts:
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
        return inserted

    def _write_manifest(self, *, source: str = "incremental") -> None:
        assert self._snapshot_dir is not None
        assert self._snapshot_id is not None
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "snapshot_id": self._snapshot_id,
            "generated_at": datetime.now().astimezone().isoformat(),
            "fact_count": len(self._facts),
            "category_counts": {
                category: sum(1 for fact in self._facts.values() if _CATEGORY_BY_REASON[fact.reason] == category)
                for category in sorted(set(_CATEGORY_BY_REASON.values()))
            },
            "manifest_hash": _manifest_hash(tuple(self._facts.values())),
            "refresh_state": self._refresh_state,
            "last_refresh_at": self._last_refresh_at.isoformat() if self._last_refresh_at else None,
            "next_refresh_at": self._next_refresh_at.isoformat() if self._next_refresh_at else None,
            "refresh_failure_count": self._refresh_failure_count,
            "last_source": source,
        }
        temporary = self._manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self._manifest_path)

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
    payload = "\n".join(issuer_eligibility_fact_hash(fact) for fact in sorted(facts)).encode("ascii")
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
    "SQLiteIssuerEligibilityRegistry",
]
