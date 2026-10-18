"""Audit active paired qfq SQLite shards and their publication checkpoint read-only."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from trader.download.domain.qfq_window import QFQ_WINDOWS
from trader.download.infra.qfq_sqlite import QFQ_SHARD_NAMES, QfqShardName
from trader.infra.sqlite_connection import OpenSqliteConnection, SqliteConnectionOptions

from .reporting import emit_report


@dataclass(frozen=True, slots=True)
class _Identity:
    source: str
    cutoff: str
    dates: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ProfileAudit:
    identities: dict[str, _Identity]
    summary: dict[str, object]
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Checkpoint:
    codes: frozenset[str]
    day: str | None
    source: str | None
    valid: bool


def _read_checkpoint(root: Path) -> _Checkpoint:
    path = root / ".checkpoint.json"
    if not path.is_file() or path.is_symlink():
        return _Checkpoint(frozenset(), None, None, False)
    try:
        payload: object = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, ValueError):
        return _Checkpoint(frozenset(), None, None, False)
    if not isinstance(payload, dict):
        return _Checkpoint(frozenset(), None, None, False)
    day, source, raw_codes = payload.get("day"), payload.get("source"), payload.get("codes")
    if not isinstance(day, str) or not isinstance(source, str) or not isinstance(raw_codes, list):
        return _Checkpoint(frozenset(), None, None, False)
    codes = tuple(raw_codes)
    valid_codes = all(isinstance(code, str) and len(code) == 6 and code.isascii() and code.isdigit() for code in codes)
    if not valid_codes or len(set(codes)) != len(codes):
        return _Checkpoint(frozenset(), day, source, False)
    return _Checkpoint(frozenset(codes), day, source, True)


def _decode_dates(value: object) -> tuple[str, ...] | None:
    try:
        decoded: object = json.loads(value) if isinstance(value, str) else None
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, list) or not decoded or not all(isinstance(day, str) for day in decoded):
        return None
    dates = tuple(decoded)
    return dates if dates == tuple(sorted(set(dates))) else None


def _routed_to_shard(code: str, name: str) -> bool:
    try:
        return bool(QfqShardName(code) == name)
    except ValueError:
        return False


def _copy_stable_shard(path: Path, target: Path) -> bool:
    journal = Path(f"{path}-journal")
    before = path.stat()
    journal_before = journal.is_file() and journal.stat().st_size > 0
    shutil.copyfile(path, target)
    after = path.stat()
    journal_after = journal.is_file() and journal.stat().st_size > 0
    return (
        not journal_before
        and not journal_after
        and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    )


def _audit_profile(root: Path, scratch: Path, profile: str, sessions: int) -> _ProfileAudit:
    profile_root = root / profile
    if profile_root.is_symlink() or not profile_root.is_dir():
        return _ProfileAudit(
            {}, {"profile": profile, "shards": 0, "codes": 0, "declared_rows": 0}, ("profile_missing",)
        )
    unexpected = tuple(
        path.name for path in profile_root.glob("*.sqlite3") if path.name not in QFQ_SHARD_NAMES or path.is_symlink()
    )
    identities: dict[str, _Identity] = {}
    declared_rows = 0
    invalid_identities = 0
    quick_check_failures = 0
    unstable_snapshots = 0
    journal_modes: set[str] = set()
    hot_journals = 0
    shard_count = 0
    for name in QFQ_SHARD_NAMES:
        path = profile_root / name
        if not path.exists():
            continue
        if not path.is_file() or path.is_symlink():
            unexpected += (name,)
            continue
        shard_count += 1
        journal = Path(f"{path}-journal")
        hot_journals += int(journal.is_file() and journal.stat().st_size > 0)
        snapshot = scratch / f"{profile}-{name}"
        if not _copy_stable_shard(path, snapshot):
            unstable_snapshots += 1
            continue
        connection = OpenSqliteConnection(snapshot, SqliteConnectionOptions(read_only=True, timeout_seconds=1))
        try:
            quick_check = tuple(connection.execute("PRAGMA quick_check"))
            quick_check_failures += int(quick_check != (("ok",),))
            journal_modes.add(str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower())
            tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"bars", "identities"}.issubset(tables):
                raise ValueError("qfq_schema_incomplete")
            for code, source, cutoff, dates_json in connection.execute(
                "SELECT code,source,cutoff,dates FROM identities ORDER BY code"
            ):
                code = str(code)
                dates = _decode_dates(dates_json)
                if dates is None:
                    invalid_identities += 1
                    continue
                valid = (
                    _routed_to_shard(code, name)
                    and len(dates) <= sessions
                    and str(cutoff) == dates[-1]
                    and code not in identities
                )
                if not valid:
                    invalid_identities += 1
                    continue
                identities[code] = _Identity(str(source), str(cutoff), dates)
                declared_rows += len(dates)
        finally:
            connection.close()
    failures: list[str] = []
    if not shard_count:
        failures.append("profile_empty")
    if unexpected:
        failures.append("unsafe_or_unexpected_shard")
    if quick_check_failures:
        failures.append("quick_check_failed")
    if unstable_snapshots:
        failures.append("publication_changed_during_audit")
    if invalid_identities:
        failures.append("identity_invalid")
    summary: dict[str, object] = {
        "profile": profile,
        "shards": shard_count,
        "codes": len(identities),
        "declared_rows": declared_rows,
        "invalid_identities": invalid_identities,
        "quick_check_failures": quick_check_failures,
        "unstable_snapshots": unstable_snapshots,
        "journal_modes": sorted(journal_modes),
        "hot_journals": hot_journals,
        "unexpected_shards": len(unexpected),
    }
    return _ProfileAudit(identities, summary, tuple(failures))


def _paired_summary(v2: _ProfileAudit, v3: _ProfileAudit, checkpoint: _Checkpoint) -> dict[str, object]:
    v2_codes, v3_codes = set(v2.identities), set(v3.identities)
    paired_codes = v2_codes & v3_codes
    source_mismatches = 0
    cutoff_mismatches = 0
    calendar_mismatches = 0
    checkpoint_source_mismatches = 0
    for code in paired_codes:
        left, right = v2.identities[code], v3.identities[code]
        source_mismatches += int(left.source != right.source)
        cutoff_mismatches += int(left.cutoff != right.cutoff)
        calendar_mismatches += int(right.dates != left.dates[-len(right.dates) :])
        if checkpoint.source is not None:
            checkpoint_source_mismatches += int(
                code in checkpoint.codes and (left.source != checkpoint.source or right.source != checkpoint.source)
            )
    return {
        "paired_codes": len(paired_codes),
        "missing_from_v2": len(v3_codes - v2_codes),
        "missing_from_v3": len(v2_codes - v3_codes),
        "source_mismatches": source_mismatches,
        "cutoff_mismatches": cutoff_mismatches,
        "calendar_mismatches": calendar_mismatches,
        "checkpoint_valid": checkpoint.valid,
        "checkpoint_day": checkpoint.day,
        "checkpoint_codes": len(checkpoint.codes),
        "database_not_checkpointed": len(paired_codes - checkpoint.codes),
        "checkpoint_not_paired": len(checkpoint.codes - paired_codes),
        "checkpoint_source_mismatches": checkpoint_source_mismatches,
    }


def AuditActiveQfq(root: Path) -> dict[str, object]:
    """Return a bounded aggregate audit without mutating active qfq data."""

    if root.is_symlink() or not root.is_dir():
        raise FileNotFoundError("qfq root missing or unsafe")
    sessions = dict(QFQ_WINDOWS)
    with TemporaryDirectory(prefix="trader-qfq-audit-") as temporary:
        scratch = Path(temporary)
        v2 = _audit_profile(root, scratch, "v2", sessions["v2"])
        v3 = _audit_profile(root, scratch, "v3", sessions["v3"])
    checkpoint = _read_checkpoint(root)
    paired = _paired_summary(v2, v3, checkpoint)
    failures = [*v2.failures, *v3.failures]
    if not checkpoint.valid:
        failures.append("checkpoint_invalid")
    mismatch_keys = (
        "missing_from_v2",
        "missing_from_v3",
        "source_mismatches",
        "cutoff_mismatches",
        "calendar_mismatches",
        "database_not_checkpointed",
        "checkpoint_not_paired",
        "checkpoint_source_mismatches",
    )
    degraded = any(bool(paired[key]) for key in mismatch_keys)
    degraded = degraded or any(
        summary["hot_journals"] or summary["journal_modes"] != ["delete"] for summary in (v2.summary, v3.summary)
    )
    status = "failed" if failures else "degraded" if degraded else "passed"
    return {
        "schema_version": "qfq-sqlite-diagnostic",
        "status": status,
        "summary": {
            "mode": "active_board_shards",
            "source_unchanged": True,
            "active_data_writes": 0,
            "profiles": (v2.summary, v3.summary),
            "paired_publication": paired,
            "failures": tuple(sorted(set(failures))),
        },
    }


def ClassifyQfqDiagnosticError(exc: BaseException) -> str:
    if isinstance(exc, sqlite3.OperationalError) and any(marker in str(exc).lower() for marker in ("locked", "busy")):
        return "qfq_pair_database_busy"
    if isinstance(exc, FileNotFoundError):
        return "qfq_root_unavailable"
    if isinstance(exc, sqlite3.DatabaseError):
        return "qfq_database_invalid"
    return "qfq_diagnostic_failed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qfq-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = AuditActiveQfq(args.qfq_root.expanduser().resolve())
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        report = {
            "schema_version": "qfq-sqlite-diagnostic",
            "status": "failed",
            "error": ClassifyQfqDiagnosticError(exc),
        }
    emit_report(report)
    return 1 if report["status"] == "failed" else 0


__all__ = ["AuditActiveQfq", "ClassifyQfqDiagnosticError"]


if __name__ == "__main__":
    raise SystemExit(main())
