"""Compare a bounded prior risk baseline with one read-only formal snapshot."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.infra.regular_file import ReadRegularFile
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.infra.sqlite_policy import SqliteIoPolicy

_BASELINE_BYTES = 4 * 1024 * 1024
_RISK_LIMIT = 10_000
_EVIDENCE_LIMIT = 50_000
_COLUMNS = (
    "code", "event_key", "risk_type", "subject_type", "level", "case_number",
    "first_disclosed_at", "last_disclosed_at", "effective_at", "status", "resolved_at", "expires_at",
)
_RiskIdentity = tuple[str, str]
_EvidenceIdentity = tuple[int, str]


@dataclass(frozen=True)
class _RiskRecord:
    code: str
    event_key: str
    risk_type: int
    subject_type: int
    level: int
    case_number: str | None
    first_disclosed_at: int
    last_disclosed_at: int
    effective_at: int
    status: int
    resolved_at: int | None
    expires_at: int | None

    @property
    def identity(self) -> _RiskIdentity:
        return self.code, self.event_key


@dataclass(frozen=True)
class _BaselineRisk:
    record: _RiskRecord
    evidence: frozenset[_EvidenceIdentity]


@dataclass(frozen=True)
class CompanyRiskChange:
    code: str
    event_key: str
    changed_fields: tuple[str, ...]
    original_evidence_retained: bool


@dataclass(frozen=True)
class CompanyRiskComparison:
    baseline_sha256: str
    baseline_risks: int
    new_risks: int
    exactly_preserved: int
    missing: tuple[_RiskIdentity, ...]
    changed_requires_review: tuple[CompanyRiskChange, ...]
    publication_hash: str | None
    stable_read: bool

    @property
    def all_old_risks_and_evidence_preserved(self) -> bool:
        return (
            self.stable_read and self.publication_hash is not None
            and not self.missing and not self.changed_requires_review
        )


def _text(value: object, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 240 or any(not char.isprintable() for char in value):
        raise ValueError("company_risk_baseline_text_invalid")
    return value


def _integer(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError("company_risk_baseline_integer_invalid")
    return value


def _optional_integer(value: object) -> int | None:
    return None if value is None else _integer(value)


def _identity(row: Mapping[str, object]) -> _RiskIdentity:
    code, event_key = _text(row.get("code")), _text(row.get("event_key"))
    if code is None or event_key is None or re.fullmatch(r"[036][0-9]{5}", code) is None:
        raise ValueError("company_risk_baseline_identity_invalid")
    return code, event_key


def _record(row: Mapping[str, object]) -> _RiskRecord:
    if not all(column in row for column in _COLUMNS):
        raise ValueError("company_risk_baseline_fields_missing")
    code, event_key = _identity(row)
    risk_type, subject_type, level, status = (_integer(row[name]) for name in (
        "risk_type", "subject_type", "level", "status",
    ))
    if not 1 <= risk_type <= 10 or not 1 <= subject_type <= 4 or not 1 <= level <= 4 or not 1 <= status <= 3:
        raise ValueError("company_risk_baseline_category_invalid")
    return _RiskRecord(
        code, event_key, risk_type, subject_type, level, _text(row["case_number"], optional=True),
        _integer(row["first_disclosed_at"]), _integer(row["last_disclosed_at"]),
        _integer(row["effective_at"]), status, _optional_integer(row["resolved_at"]),
        _optional_integer(row["expires_at"]),
    )


def _baseline(path: Path, check: Callable[[], None]) -> tuple[str, tuple[_BaselineRisk, ...]]:
    check()
    content = ReadRegularFile(path, maximum_bytes=_BASELINE_BYTES)
    check()
    try:
        payload: object = json.loads(content)
    except (ValueError, RecursionError) as exc:
        raise ValueError("company_risk_baseline_json_invalid") from exc
    check()
    if not isinstance(payload, Mapping):
        raise ValueError("company_risk_baseline_object_required")
    risks, evidence = payload.get("risks"), payload.get("evidence")
    if not isinstance(risks, list) or not isinstance(evidence, list):
        raise ValueError("company_risk_baseline_rows_required")
    if len(risks) > _RISK_LIMIT or len(evidence) > _EVIDENCE_LIMIT:
        raise ValueError("company_risk_baseline_rows_limit")
    records: dict[_RiskIdentity, _RiskRecord] = {}
    links: dict[_RiskIdentity, set[_EvidenceIdentity]] = {}
    for row in risks:
        check()
        if not isinstance(row, Mapping):
            raise ValueError("company_risk_baseline_row_invalid")
        item = _record(row)
        if item.identity in records:
            raise ValueError("company_risk_baseline_duplicate_identity")
        records[item.identity] = item
    for row in evidence:
        check()
        if not isinstance(row, Mapping):
            raise ValueError("company_risk_baseline_evidence_invalid")
        identity = _identity(row)
        source, source_key = _integer(row.get("source")), _text(row.get("source_key"))
        if identity not in records or not 1 <= source <= 10 or source_key is None:
            raise ValueError("company_risk_baseline_evidence_invalid")
        links.setdefault(identity, set()).add((source, source_key))
    check()
    if records.keys() - links.keys():
        raise ValueError("company_risk_baseline_original_evidence_missing")
    return hashlib.sha256(content).hexdigest(), tuple(
        _BaselineRisk(record, frozenset(links[identity])) for identity, record in sorted(records.items())
    )


def _changed_fields(before: _RiskRecord, after: _RiskRecord) -> tuple[str, ...]:
    return tuple(name for name, first, second in (
        ("risk_type", before.risk_type, after.risk_type),
        ("subject_type", before.subject_type, after.subject_type),
        ("level", before.level, after.level),
        ("case_number", before.case_number, after.case_number),
        ("first_disclosed_at", before.first_disclosed_at, after.first_disclosed_at),
        ("last_disclosed_at", before.last_disclosed_at, after.last_disclosed_at),
        ("effective_at", before.effective_at, after.effective_at),
        ("status", before.status, after.status),
        ("resolved_at", before.resolved_at, after.resolved_at),
        ("expires_at", before.expires_at, after.expires_at),
    ) if first != second)


def WriteCompanyRiskBaseline(
    facts_path: Path, target: Path, *, deadline: float, sqlite_policy: SqliteIoPolicy,
) -> None:
    """Export the existing bounded risk-baseline protocol from one formal snapshot."""
    def check() -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("company_risk_baseline_deadline_exceeded")

    check()
    with SqliteSession(
        facts_path, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy,
        cancel_requested=lambda: time.monotonic() >= deadline,
    ) as connection, SqliteTransaction(connection):
        if ReadCompanyPublicationIdentity(connection) is None:
            raise ValueError("company_risk_baseline_publication_missing")
        risks: list[dict[str, object]] = []
        for row in connection.run(f"SELECT {','.join(_COLUMNS)} FROM company_risk_event ORDER BY code,event_key"):
            check()
            if len(risks) >= _RISK_LIMIT:
                raise ValueError("company_risk_baseline_risk_limit")
            risks.append(dict(zip(_COLUMNS, row, strict=True)))
        evidence: list[dict[str, object]] = []
        for row in connection.run(
            "SELECT r.code,r.event_key,e.source,e.source_key FROM company_risk_event r "
            "JOIN company_risk_evidence re ON re.risk_id=r.id "
            "JOIN company_evidence_ref e ON e.id=re.evidence_id ORDER BY r.code,r.event_key,e.source,e.source_key"
        ):
            check()
            if len(evidence) >= _EVIDENCE_LIMIT:
                raise ValueError("company_risk_baseline_evidence_limit")
            evidence.append(dict(zip(("code", "event_key", "source", "source_key"), row, strict=True)))
    content = json.dumps({"risks": risks, "evidence": evidence}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(content) > _BASELINE_BYTES:
        raise ValueError("company_risk_baseline_size_limit")
    check()
    with target.open("xb") as stream:
        stream.write(content)
    _baseline(target, check)


def CompareCompanyRisks(
    facts_path: Path,
    baseline_path: Path,
    *,
    deadline: float,
    sqlite_policy: SqliteIoPolicy,
    cancel_requested: Callable[[], bool],
) -> CompanyRiskComparison:
    """Compare fields and original evidence; a missing risk is never treated as resolved."""
    def check() -> None:
        if cancel_requested() or time.monotonic() >= deadline:
            # The owner classifies this only if its own deadline callback fired.
            cancel_requested()
            raise sqlite3.OperationalError("interrupted")

    check()
    digest, risks = _baseline(baseline_path, check)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        cancel_requested()
        raise sqlite3.OperationalError("interrupted")
    policy = replace(
        sqlite_policy,
        connection_timeout_seconds=min(sqlite_policy.connection_timeout_seconds, remaining),
    )
    missing: list[_RiskIdentity] = []
    changed: list[CompanyRiskChange] = []
    preserved = evidence_rows = 0
    with SqliteSession(
        facts_path, SqliteConnectionOptions(read_only=True),
        cancel_requested=cancel_requested, sqlite_policy=policy,
    ) as connection:
        version = connection.run("PRAGMA data_version").require_one()[0]
        with SqliteTransaction(connection):
            identity = ReadCompanyPublicationIdentity(connection)
            total = connection.run("SELECT COUNT(*) FROM company_risk_event").require_one()[0]
            for baseline in risks:
                check()
                row = connection.run(
                    f"SELECT {','.join(_COLUMNS)} FROM company_risk_event WHERE code=? AND event_key=?",
                    baseline.record.identity,
                ).fetchone()
                if row is None:
                    missing.append(baseline.record.identity)
                    continue
                current = _record(dict(zip(_COLUMNS, row, strict=True)))
                links: set[_EvidenceIdentity] = set()
                for link in connection.run(
                    "SELECT e.source,e.source_key FROM company_risk_event r "
                    "JOIN company_risk_evidence re ON re.risk_id=r.id "
                    "JOIN company_evidence_ref e ON e.id=re.evidence_id WHERE r.code=? AND r.event_key=?",
                    current.identity,
                ):
                    check()
                    evidence_rows += 1
                    if evidence_rows > _EVIDENCE_LIMIT:
                        raise ValueError("company_risk_comparison_evidence_limit")
                    source_key = _text(link[1])
                    if source_key is None:
                        raise ValueError("company_risk_comparison_evidence_invalid")
                    links.add((_integer(link[0]), source_key))
                fields = _changed_fields(baseline.record, current)
                retained = baseline.evidence.issubset(links)
                if fields or not retained:
                    changed.append(CompanyRiskChange(current.code, current.event_key, fields, retained))
                else:
                    preserved += 1
        stable = version == connection.run("PRAGMA data_version").require_one()[0]
    check()
    return CompanyRiskComparison(
        digest, len(risks), _integer(total), preserved, tuple(missing), tuple(changed),
        identity.content_hash.hex() if identity else None, stable,
    )


def ProjectCompanyRiskComparison(comparison: CompanyRiskComparison) -> dict[str, object]:
    """Expose comparison identities and field names, never raw risk values or case text."""
    return {
        "read_only": True, "complete": comparison.stable_read and comparison.publication_hash is not None,
        "stable_read": comparison.stable_read,
        "baseline_sha256": comparison.baseline_sha256, "baseline_risks": comparison.baseline_risks,
        "new_risks": comparison.new_risks, "exactly_preserved": comparison.exactly_preserved,
        "publication_hash": comparison.publication_hash,
        "missing": [{"code": code, "event_key": event_key} for code, event_key in comparison.missing],
        "changed_requires_review": [
            {"code": item.code, "event_key": item.event_key, "changed_fields": list(item.changed_fields),
             "original_evidence_retained": item.original_evidence_retained}
            for item in comparison.changed_requires_review
        ],
        "all_old_risks_and_evidence_preserved": comparison.all_old_risks_and_evidence_preserved,
        "missing_means_resolved": False, "field_correctness_verified": False,
    }


__all__ = [
    "CompanyRiskChange",
    "CompanyRiskComparison",
    "CompareCompanyRisks",
    "ProjectCompanyRiskComparison",
    "WriteCompanyRiskBaseline",
]
