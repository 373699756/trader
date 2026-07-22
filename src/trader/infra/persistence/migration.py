"""Read-only v2 archive importer for the isolated v17 runtime."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from trader.infra.persistence.snapshots import snapshot_from_dict
from trader.infra.persistence.writer import SnapshotRepository


def migrate_v17_archive(source_runtime: Path, target_runtime: Path) -> dict[str, object]:
    source = source_runtime.resolve()
    target = target_runtime.resolve()
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("source and target runtime directories must be isolated")
    database = source / "runtime.sqlite3"
    if not database.is_file():
        raise ValueError("source runtime database does not exist")
    before = _tree_digest(source)
    uri = f"file:{database.as_posix()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT relative_path, sha256 FROM frozen_snapshots
            WHERE status='committed' ORDER BY recommend_date, strategy
            """
        ).fetchall()
    imported = 0
    existing = 0
    for row in rows:
        path = source / str(row["relative_path"])
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != str(row["sha256"]):
            raise ValueError(f"source frozen snapshot hash mismatch: {path.name}")
        raw = json.loads(payload)
        if not isinstance(raw, dict):
            raise ValueError("source frozen snapshot root must be an object")
        snapshot = snapshot_from_dict(raw)
        repository = SnapshotRepository(target, config_version=snapshot.config_version)
        repository.initialize()
        if repository.load_frozen(snapshot.strategy, snapshot.trade_date) is not None:
            existing += 1
            continue
        repository.freeze(snapshot)
        imported += 1
    after = _tree_digest(source)
    if before != after:
        raise RuntimeError("source runtime changed during migration")
    return {
        "status": "ok",
        "source_digest": before,
        "source_read_only_verified": True,
        "imported": imported,
        "existing": existing,
        "ignored_published_drafts": True,
    }


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


__all__ = ["migrate_v17_archive"]
