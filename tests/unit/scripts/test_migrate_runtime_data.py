from __future__ import annotations

import json
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts.migrate_runtime_data import MigrationError, inspect_layout, migrate, rollback
from tests.unit.infra.test_decision_records import NOW, decision, record
from trader.infra.sqlite_connection import OpenSqliteConnection
from trader.recommendation.application.ports.decision_records import DecisionCheckpoint, DecisionRecordConflictError
from trader.recommendation.domain.market.eligibility import (
    IssuerEligibilityFact,
    IssuerEligibilityFactHash,
    IssuerEligibilityReason,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.persistence.decision_records import SQLiteDecisionRecords


def _source(root: Path) -> Path:
    source = root / "source"
    (source / "partitions" / "2026").mkdir(parents=True)
    (source / "partitions" / "2026" / "09.sqlite3").touch()
    with closing(OpenSqliteConnection(source / "control.sqlite3")) as connection, connection:
        connection.execute("create table identity (value text)")
        connection.execute("insert into identity values ('test')")
    return source


def test_migration_validates_and_publishes_atomic_copy(tmp_path: Path) -> None:
    source = _source(tmp_path)
    target = tmp_path / "isolated" / "history"

    manifest = migrate(source, target)

    assert target.is_dir()
    assert (target / "migration-manifest.json").is_file()
    assert manifest.file_count == 2
    assert not (target.parent / ".history.migration.lock").exists()


def test_migration_is_reversible_with_retained_backup(tmp_path: Path) -> None:
    source = _source(tmp_path)
    target = tmp_path / "isolated" / "history"
    migrate(source, target)
    source.joinpath("new.txt").write_text("new", encoding="utf-8")
    migrate(source, target)
    backup = target.parent / ".history.previous"

    rollback(target, backup)

    assert not (target / "new.txt").exists()
    assert (target / "control.sqlite3").is_file()


def test_migration_rejects_repository_paths() -> None:
    with pytest.raises(MigrationError, match="outside the repository"):
        inspect_layout(Path(__file__).resolve().parents[3])


def _legacy_freeze(root: Path) -> Path:
    source = root / "source"
    for profile in ("v2", "v3"):
        legacy = source / profile / "decisions"
        records = SQLiteDecisionRecords(legacy)
        records.initialize()
        records.commit(record())
        records.commit(record(Strategy.D25))
        boundary = NOW.replace(hour=15, minute=0)
        records.save_checkpoint(
            DecisionCheckpoint(replace(decision(), observed_at=boundary - timedelta(seconds=20)), boundary)
        )
        for strategy in (Strategy.TOMORROW, Strategy.D25):
            expected = record(strategy)
            relative = Path("records") / strategy.value / NOW.date().isoformat() / f"{expected.version}.json"
            target = legacy / relative
            target.parent.mkdir(parents=True)
            (legacy / strategy.value / f"{NOW.date().isoformat()}.json").rename(target)
            (legacy / strategy.value).rmdir()
            with closing(OpenSqliteConnection(legacy / "freeze.sqlite3")) as connection, connection:
                connection.execute(
                    "UPDATE decision_records SET relative_path = ? WHERE strategy = ?",
                    (relative.as_posix(), strategy.value),
                )
        (legacy / "freeze.sqlite3").rename(legacy / "decisions.sqlite3")
        (legacy / "quarantine").mkdir()
    return source


def test_freeze_layout_migration_preserves_bytes_checkpoints_and_first_wins(tmp_path: Path) -> None:
    source = _legacy_freeze(tmp_path)
    for profile in ("v2", "v3"):
        database = source / profile / "decisions" / "decisions.sqlite3"
        database.with_name(f"{database.name}-wal").touch()
        database.with_name(f"{database.name}-shm").write_bytes(b"disposable shared memory")
    original = {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    target = tmp_path / "target"

    manifest = migrate(source, target, freeze_layout=True)

    assert manifest.schema_version == "trader_freeze_layout_migration"
    assert {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()} == original
    for profile in ("v2", "v3"):
        records = SQLiteDecisionRecords(target / profile)
        records.initialize()
        for strategy in (Strategy.TOMORROW, Strategy.D25):
            expected = record(strategy)
            relative = Path(profile) / "decisions" / "records" / strategy.value / NOW.date().isoformat()
            assert (target / profile / strategy.value / f"{NOW.date().isoformat()}.json").read_bytes() == original[
                relative / f"{expected.version}.json"
            ]
            assert records.load(strategy, NOW.date()) == expected
            assert records.list_dates(strategy) == (NOW.date(),)
        with pytest.raises(DecisionRecordConflictError):
            records.commit(record(sequence=3))
        assert records.load_checkpoint(Strategy.TOMORROW, NOW.date()) is not None
        assert not (target / profile / "decisions").exists()
        assert not (target / profile / "quarantine").exists()
        assert not tuple((target / profile).glob("*-shm"))
        assert not tuple((target / profile).glob("*-wal"))


@pytest.mark.parametrize("problem", ("corrupt", "orphan", "journal", "staged"))
def test_freeze_migration_rejects_unverified_source_without_replacing_target(tmp_path: Path, problem: str) -> None:
    source = _legacy_freeze(tmp_path)
    legacy = source / "v2" / "decisions"
    if problem == "corrupt":
        next((legacy / "records").rglob("*.json")).write_bytes(b"corrupt")
    elif problem == "orphan":
        (legacy / "records" / "orphan.json").write_bytes(b"orphan")
    elif problem == "journal":
        (legacy / "decisions.sqlite3-wal").write_bytes(b"uncheckpointed")
    else:
        with closing(OpenSqliteConnection(legacy / "decisions.sqlite3")) as connection, connection:
            connection.execute("UPDATE decision_records SET status = 'staged'")
    target = tmp_path / "target"
    target.mkdir()
    marker = target / "retained"
    marker.write_bytes(b"untouched")

    with pytest.raises((MigrationError, OSError)):
        migrate(source, target, freeze_layout=True)

    assert marker.read_bytes() == b"untouched"
    assert not (target / "v2").exists()


def test_reference_migration_keeps_generated_evidence_converts_eligibility_and_drops_latest_projection(
    tmp_path: Path,
) -> None:
    source = tmp_path / "reference-source"
    source.mkdir()
    (source / "historical_st.json").write_text(json.dumps({"600000": ["2026-01-01", "ever_st"]}), encoding="utf-8")
    with closing(OpenSqliteConnection(source / "industry_queries.sqlite3")) as connection, connection:
        connection.execute("create table facts (code text primary key)")
    (source / "industry_mapping.json").write_text("{}", encoding="utf-8")
    eligibility = tmp_path / "eligibility.sqlite3"
    fact = IssuerEligibilityFact(
        "600000",
        IssuerEligibilityReason.HISTORICAL_ST,
        datetime(2026, 1, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
        "fixture:600000",
        "fixture",
        "a" * 64,
    )
    with closing(OpenSqliteConnection(eligibility)) as connection, connection:
        connection.execute(
            """
            create table issuer_eligibility_facts (
                code text, reason text, effective_at text, evidence_id text,
                source text, evidence_hash text, content_hash text
            )
            """
        )
        connection.execute(
            "insert into issuer_eligibility_facts values (?,?,?,?,?,?,?)",
            (
                fact.code,
                fact.reason.value,
                fact.effective_at.isoformat(),
                fact.evidence_id,
                fact.source,
                fact.evidence_hash,
                IssuerEligibilityFactHash(fact),
            ),
        )
    target = tmp_path / "isolated" / "reference"

    manifest = migrate(source, target, reference_layout=True, eligibility_database=eligibility)

    assert manifest.schema_version == "trader_reference_layout_migration"
    assert (target / "historical_st.json").is_file()
    assert (target / "industry_queries.sqlite3").is_file()
    assert (target / "active-manifest.json").is_file()
    assert not (target / "industry_mapping.json").exists()
