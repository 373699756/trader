from __future__ import annotations

import sqlite3
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts.migrate_runtime_data import MigrationError, inspect_layout, migrate, rollback
from trader.recommendation.domain.market.eligibility import (
    IssuerEligibilityFact,
    IssuerEligibilityReason,
    issuer_eligibility_fact_hash,
)


def _source(root: Path) -> Path:
    source = root / "source"
    (source / "partitions" / "2026").mkdir(parents=True)
    (source / "partitions" / "2026" / "09.sqlite3").touch()
    with sqlite3.connect(source / "control.sqlite3") as connection:
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


def test_reference_migration_keeps_generated_evidence_converts_eligibility_and_drops_latest_projection(
    tmp_path: Path,
) -> None:
    source = tmp_path / "reference-source"
    source.mkdir()
    (source / "historical_st.json").write_text(json.dumps({"600000": ["2026-01-01", "ever_st"]}), encoding="utf-8")
    with sqlite3.connect(source / "industry_queries.sqlite3") as connection:
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
    with sqlite3.connect(eligibility) as connection:
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
                issuer_eligibility_fact_hash(fact),
            ),
        )
    target = tmp_path / "isolated" / "reference"

    manifest = migrate(source, target, reference_layout=True, eligibility_database=eligibility)

    assert manifest.schema_version == "trader_reference_layout_migration"
    assert (target / "historical_st.json").is_file()
    assert (target / "industry_queries.sqlite3").is_file()
    assert (target / "active-manifest.json").is_file()
    assert not (target / "industry_mapping.json").exists()
