from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.runtime_diagnostics import qfq_sqlite
from scripts.runtime_diagnostics.qfq_sqlite import AuditActiveQfq, ClassifyQfqDiagnosticError
from trader.download.infra.qfq_sqlite import QfqShardName


_SCHEMA = """
CREATE TABLE bars(code TEXT NOT NULL, day TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(code,day)) WITHOUT ROWID;
CREATE TABLE identities(code TEXT PRIMARY KEY, digest TEXT NOT NULL, source TEXT NOT NULL,
 cutoff TEXT NOT NULL, dates TEXT NOT NULL) WITHOUT ROWID;
"""


def _seed_profile(root: Path, profile: str, code: str, dates: tuple[str, ...], source: str = "fixture") -> None:
    profile_root = root / profile
    profile_root.mkdir(parents=True, exist_ok=True)
    path = profile_root / QfqShardName(code)
    with sqlite3.connect(path) as connection:
        connection.executescript(_SCHEMA)
        connection.executemany("INSERT INTO bars VALUES (?,?,?)", ((code, day, "{}") for day in dates))
        connection.execute(
            "INSERT INTO identities VALUES (?,?,?,?,?)",
            (code, "digest", source, dates[-1], json.dumps(dates, separators=(",", ":"))),
        )


def _checkpoint(root: Path, codes: tuple[str, ...], source: str = "fixture") -> None:
    (root / ".checkpoint.json").write_text(
        json.dumps({"day": "2026-10-16", "source": source, "codes": codes}), encoding="ascii"
    )


def test_active_qfq_audit_reports_paired_checkpoint_without_writes(tmp_path: Path) -> None:
    long_dates = ("2026-10-14", "2026-10-15", "2026-10-16")
    _seed_profile(tmp_path, "v2", "600001", long_dates)
    _seed_profile(tmp_path, "v3", "600001", long_dates[-2:])
    _checkpoint(tmp_path, ("600001",))
    before = {path: path.stat().st_mtime_ns for path in tmp_path.rglob("*") if path.is_file()}

    report = AuditActiveQfq(tmp_path)

    assert report["status"] == "passed"
    summary = report["summary"]
    assert isinstance(summary, dict)
    assert summary["mode"] == "active_board_shards"
    assert summary["active_data_writes"] == 0
    paired = summary["paired_publication"]
    assert isinstance(paired, dict)
    assert paired == {
        "paired_codes": 1,
        "missing_from_v2": 0,
        "missing_from_v3": 0,
        "source_mismatches": 0,
        "cutoff_mismatches": 0,
        "calendar_mismatches": 0,
        "checkpoint_valid": True,
        "checkpoint_day": "2026-10-16",
        "checkpoint_codes": 1,
        "database_not_checkpointed": 0,
        "checkpoint_not_paired": 0,
        "checkpoint_source_mismatches": 0,
    }
    assert "600001" not in json.dumps(report)
    assert before == {path: path.stat().st_mtime_ns for path in tmp_path.rglob("*") if path.is_file()}


def test_active_qfq_audit_distinguishes_database_and_checkpoint_gaps(tmp_path: Path) -> None:
    dates = ("2026-10-15", "2026-10-16")
    _seed_profile(tmp_path, "v2", "600001", dates)
    _seed_profile(tmp_path, "v3", "600001", dates)
    _checkpoint(tmp_path, ())

    report = AuditActiveQfq(tmp_path)

    assert report["status"] == "degraded"
    summary = report["summary"]
    assert isinstance(summary, dict)
    paired = summary["paired_publication"]
    assert isinstance(paired, dict)
    assert paired["database_not_checkpointed"] == 1
    assert paired["checkpoint_not_paired"] == 0


def test_qfq_audit_preserves_stable_busy_reason() -> None:
    assert ClassifyQfqDiagnosticError(sqlite3.OperationalError("database is locked")) == "qfq_pair_database_busy"


def test_active_qfq_audit_rejects_a_shard_that_changes_during_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dates = ("2026-10-15", "2026-10-16")
    _seed_profile(tmp_path, "v2", "600001", dates)
    _seed_profile(tmp_path, "v3", "600001", dates)
    _checkpoint(tmp_path, ("600001",))
    monkeypatch.setattr(qfq_sqlite, "_copy_stable_shard", lambda _path, _target: False)

    report = AuditActiveQfq(tmp_path)

    assert report["status"] == "failed"
    summary = report["summary"]
    assert isinstance(summary, dict)
    assert "publication_changed_during_audit" in summary["failures"]
