from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from tests.helpers.qfq import qfq_window
from trader.download.infra.history_control_index import HistoryMaintenanceLock
from trader.download.infra.qfq_layout_migration import BuildQfqLayout
from trader.download.infra.qfq_sqlite import QFQ_SHARD_NAMES, QfqShardName, SQLiteQfqWindowCache
from trader.infra.atomic_files.json import AtomicWriteJson


def _legacy(root: Path) -> None:
    for profile in ("v2", "v3"):
        cache = SQLiteQfqWindowCache(root, profile)
        codes = ("600001", "000001", "300001", "688001")
        for code in codes:
            cache.replace_window(qfq_window(code), "fixture:legacy")
        names = {name: f"{index:05d}.sqlite3" for index, name in enumerate(QFQ_SHARD_NAMES)}
        for name, old in names.items():
            (cache.root / name).rename(cache.root / old)
        AtomicWriteJson(cache.root / "index.json", {code: names[QfqShardName(code)] for code in codes})


@pytest.fixture(scope="module")
def legacy_seed(tmp_path_factory):
    root = tmp_path_factory.mktemp("qfq-migration-seed") / "legacy"
    _legacy(root)
    return root


def _files(root: Path):
    return {
        str(path.relative_to(root)): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def test_migration_preserves_windows_and_source_without_activating(tmp_path, legacy_seed):
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    shutil.copytree(legacy_seed, source)
    before = _files(source)
    result = BuildQfqLayout(source, target, tmp_path / ".lock")
    assert result.codes == 8 and result.rows == 4 * (251 + 61)
    assert result.source_files == result.target_files == 8
    assert _files(source) == before
    for profile in ("v2", "v3"):
        cache = SQLiteQfqWindowCache(target, profile)
        for code in ("600001", "000001", "300001", "688001"):
            window = qfq_window(code)
            assert cache.read_code(code).cells == window.cells[-cache.sessions :]
            assert cache.source_identity(code) == "fixture:legacy"
        assert not (cache.root / "index.json").exists()
    with pytest.raises(ValueError, match="must be absent"):
        BuildQfqLayout(source, target, tmp_path / ".lock")


def test_corrupt_source_never_publishes_target_or_changes_source(tmp_path, legacy_seed):
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    shutil.copytree(legacy_seed, source)
    with sqlite3.connect(source / "v3/00001.sqlite3") as connection:
        connection.execute("UPDATE identities SET digest='tampered'")
    before = _files(source)
    with pytest.raises(ValueError, match="identity mismatch"):
        BuildQfqLayout(source, target, tmp_path / ".lock")
    assert not target.exists()
    assert _files(source) == before


def test_changed_source_before_publication_is_rejected(tmp_path, monkeypatch, legacy_seed):
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    shutil.copytree(legacy_seed, source)
    calls = iter(("original", "changed"))
    monkeypatch.setattr("trader.download.infra.qfq_layout_migration.LegacyFingerprint", lambda _: next(calls))
    with pytest.raises(ValueError, match="source changed"):
        BuildQfqLayout(source, target, tmp_path / ".lock")
    assert not target.exists()


def test_path_traversal_and_overlap_are_rejected(tmp_path, legacy_seed):
    source = tmp_path / "legacy"
    shutil.copytree(legacy_seed, source)
    with pytest.raises(ValueError, match="overlap"):
        BuildQfqLayout(source, source / "target", tmp_path / ".lock")
    AtomicWriteJson(source / "v2/index.json", {"600001": "../../outside.sqlite3"})
    with pytest.raises(ValueError, match="routing invalid"):
        BuildQfqLayout(source, tmp_path / "target", tmp_path / ".lock")


@pytest.mark.parametrize("lock_owner", ("history", "qfq"))
def test_migration_cli_locks_explicit_qfq_source_independently_of_history(
    tmp_path, monkeypatch, capsys, lock_owner, legacy_seed
):
    from scripts.rename_qfq_shards import main

    project = tmp_path / "project"
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    shutil.copytree(legacy_seed, source)
    monkeypatch.setattr(
        "sys.argv",
        [
            "rename_qfq_shards.py",
            "--apply",
            "--project-root",
            str(project),
            "--source-root",
            str(source),
            "--target-root",
            str(target),
        ],
    )
    lock = source / ".maintenance.lock" if lock_owner == "qfq" else project / "data/history/.maintenance.lock"
    with HistoryMaintenanceLock(lock):
        status = main()
    report = json.loads(capsys.readouterr().out)
    if lock_owner == "qfq":
        assert status == 1 and report["reason"] == "HistoryMaintenanceAlreadyRunningError"
        assert not target.exists() and not target.with_name("target.building").exists()
    else:
        assert status == 0 and report["status"] == "built_verified"
        assert report["codes"] == 8 and report["source_unchanged"]
        assert len(SQLiteQfqWindowCache(target, "v2").read_code("600001").cells) == 251
