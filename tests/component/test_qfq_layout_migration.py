from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from tests.component.test_qfq_windows import _window
from trader.download.infra.qfq_layout_migration import build_qfq_layout
from trader.download.infra.qfq_sqlite import QFQ_SHARD_NAMES, SQLiteQfqWindowCache, qfq_shard_name
from trader.infra.atomic_files.json import atomic_write_json


def _legacy(root: Path) -> None:
    for profile in ("v2", "v3"):
        cache = SQLiteQfqWindowCache(root, profile)
        codes = ("600001", "000001", "300001", "688001")
        for code in codes:
            cache.replace_window(_window(code), "fixture:legacy")
        names = {name: f"{index:05d}.sqlite3" for index, name in enumerate(QFQ_SHARD_NAMES)}
        for name, old in names.items():
            (cache.root / name).rename(cache.root / old)
        atomic_write_json(cache.root / "index.json", {code: names[qfq_shard_name(code)] for code in codes})


def _files(root: Path):
    return {
        str(path.relative_to(root)): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def test_migration_preserves_windows_and_source_without_activating(tmp_path):
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    _legacy(source)
    before = _files(source)
    result = build_qfq_layout(source, target, tmp_path / ".lock")
    assert result.codes == 8 and result.rows == 4 * (251 + 61)
    assert result.source_files == result.target_files == 8
    assert _files(source) == before
    for profile in ("v2", "v3"):
        cache = SQLiteQfqWindowCache(target, profile)
        for code in ("600001", "000001", "300001", "688001"):
            window = _window(code)
            assert cache.read_code(code).cells == window.cells[-cache.sessions :]
            assert cache.source_identity(code) == "fixture:legacy"
        assert not (cache.root / "index.json").exists()
    with pytest.raises(ValueError, match="must be absent"):
        build_qfq_layout(source, target, tmp_path / ".lock")


def test_corrupt_source_never_publishes_target_or_changes_source(tmp_path):
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    _legacy(source)
    with sqlite3.connect(source / "v3/00001.sqlite3") as connection:
        connection.execute("UPDATE identities SET digest='tampered'")
    before = _files(source)
    with pytest.raises(ValueError, match="identity mismatch"):
        build_qfq_layout(source, target, tmp_path / ".lock")
    assert not target.exists()
    assert _files(source) == before


def test_changed_source_before_publication_is_rejected(tmp_path, monkeypatch):
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    _legacy(source)
    calls = iter(("original", "changed"))
    monkeypatch.setattr("trader.download.infra.qfq_layout_migration.legacy_fingerprint", lambda _: next(calls))
    with pytest.raises(ValueError, match="source changed"):
        build_qfq_layout(source, target, tmp_path / ".lock")
    assert not target.exists()


def test_path_traversal_and_overlap_are_rejected(tmp_path):
    source = tmp_path / "legacy"
    _legacy(source)
    with pytest.raises(ValueError, match="overlap"):
        build_qfq_layout(source, source / "target", tmp_path / ".lock")
    atomic_write_json(source / "v2/index.json", {"600001": "../../outside.sqlite3"})
    with pytest.raises(ValueError, match="routing invalid"):
        build_qfq_layout(source, tmp_path / "target", tmp_path / ".lock")
