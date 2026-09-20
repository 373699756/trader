from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts.migrate_runtime_data import MigrationError, inspect_layout, migrate, rollback


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
