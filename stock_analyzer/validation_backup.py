from __future__ import annotations

import hashlib
import gzip
import os
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from typing import Dict, List


_FILE_EXTENSIONS = (".db", ".sqlite", ".sqlite3")


def _snapshot_payload_path(backup_path: str) -> str:
    if os.path.isdir(backup_path):
        return os.path.join(backup_path, "latest.sqlite3")
    return os.path.abspath(backup_path)


def _archive_root(backup_path: str) -> str:
    if os.path.isdir(backup_path):
        return os.path.join(os.path.abspath(backup_path), ".history")
    return os.path.join(os.path.dirname(os.path.abspath(backup_path) or "."), ".history")


def _hash_file(path: str, sample_bytes: int = 1024 * 1024) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(sample_bytes)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _ensure_backup_dirs(target_path: str) -> None:
    os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)


def backup_validation_db(db_path: str, backup_path: str, label: str = "manual", keep: int = 1) -> Dict[str, object]:
    if not os.path.exists(db_path):
        return {"ok": False, "error": "validation_db_missing", "db_path": db_path}
    target = _snapshot_payload_path(backup_path)
    _ensure_backup_dirs(target)
    temp_path = f"{target}.tmp"
    archive_root = _archive_root(backup_path)
    manifest = {
        "ok": True,
        "path": target,
        "label": str(label or "manual"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "bytes": 0,
        "deduplicated": False,
        "mode": "single_file",
        "history": [],
        "removed": [],
    }
    try:
        _sqlite_backup(db_path, temp_path)
        same_as_existing = False
        if os.path.exists(target):
            try:
                same_as_existing = _hash_file(target) == _hash_file(temp_path)
            except Exception:
                same_as_existing = False
        if same_as_existing:
            manifest["deduplicated"] = True
            manifest["bytes"] = os.path.getsize(target)
        else:
            os.replace(temp_path, target)
            manifest["bytes"] = os.path.getsize(target)

        manifest["history"] = _append_history_if_needed(
            target,
            archive_root,
            label=str(label or "manual"),
            keep=int(keep or 0),
        )
        manifest["removed"] = [item["path"] for item in manifest["history"] if item.get("removed")]
        manifest["history"] = [item for item in manifest["history"] if not item.get("removed")]
        return manifest
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def _append_history_if_needed(
    source_path: str,
    archive_root: str,
    label: str,
    keep: int,
) -> List[Dict[str, object]]:
    os.makedirs(archive_root, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_name = f"{timestamp}_{label or 'backup'}.sqlite3.gz"
    archive_target = os.path.join(archive_root, archive_name)
    history: List[Dict[str, object]] = []

    if os.path.abspath(os.path.dirname(source_path)) == os.path.abspath(archive_root):
        # Already in history directory mode, skip canonical duplication.
        return history

    duplicate = False
    if not os.path.exists(source_path):
        return history
    source_hash = _hash_file(source_path)
    for existing in sorted(os.listdir(archive_root), reverse=True):
        if not existing.endswith(".sqlite3.gz"):
            continue
        existing_path = os.path.join(archive_root, existing)
        try:
            existing_hash = _hash_payload_hash(existing_path)
            if source_hash == existing_hash:
                duplicate = True
                break
        except Exception:
            continue
    if not duplicate and os.path.exists(source_path):
        _compress_copy(source_path, archive_target)
        history.append(
            {
                "path": archive_target,
                "bytes": os.path.getsize(archive_target),
                "removed": False,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "source": os.path.basename(source_path),
            }
        )
        for removed_path in _prune_history(archive_root, keep_days=max(1, int(keep or 0))):
            history.append({"path": removed_path, "removed": True})
        return history
    return []


def _compress_copy(source_path: str, target_gz_path: str) -> None:
    with open(source_path, "rb") as source, gzip.open(target_gz_path, "wb") as target:
        shutil.copyfileobj(source, target)


def _hash_payload_hash(archive_gz_path: str) -> str:
    # 历史文件只保留压缩版本，按完整 gzip 内容计算 hash，足够用于去重比较。
    hasher = hashlib.sha256()
    with gzip.open(archive_gz_path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _prune_history(archive_root: str, keep_days: int) -> List[str]:
    if not os.path.isdir(archive_root):
        return []
    cutoff = datetime.now() - timedelta(days=max(1, int(keep_days or 1)))
    removed: List[str] = []
    for name in sorted(os.listdir(archive_root)):
        if not name.endswith(".sqlite3.gz"):
            continue
        path = os.path.join(archive_root, name)
        try:
            if datetime.fromtimestamp(os.stat(path).st_mtime) >= cutoff:
                continue
            os.remove(path)
            removed.append(path)
        except FileNotFoundError:
            continue
    return removed


def restore_validation_db(backup_path: str, db_path: str, safety_backup_path: str = "", keep: int = 1) -> Dict[str, object]:
    if not os.path.exists(backup_path):
        return {"ok": False, "error": "backup_missing", "backup_path": backup_path}

    backup_file = _resolve_backup_file(backup_path)
    if not os.path.exists(backup_file):
        return {"ok": False, "error": "backup_file_invalid", "backup_path": backup_path}

    safety = None
    if os.path.exists(db_path) and safety_backup_path and os.path.abspath(safety_backup_path) != os.path.abspath(backup_file):
        safety = backup_validation_db(db_path, safety_backup_path, label="pre_restore", keep=keep)
        if not safety.get("ok"):
            return {"ok": False, "error": "pre_restore_backup_failed", "backup": safety}
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    temp_path = f"{db_path}.restore_tmp"
    restore_source = backup_file
    cleanup_paths: List[str] = []
    if backup_file.endswith(".gz"):
        restore_source = f"{backup_file}.restore_tmp.sqlite3"
        cleanup_paths.append(restore_source)
        with gzip.open(backup_file, "rb") as source, open(restore_source, "wb") as target:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                target.write(chunk)
    try:
        _sqlite_backup(restore_source, temp_path)
        os.replace(temp_path, db_path)
        return {
            "ok": True,
            "restored_from": backup_file,
            "db_path": db_path,
            "bytes": os.path.getsize(db_path),
            "pre_restore_backup": safety,
        }
    finally:
        for cleanup in cleanup_paths:
            if os.path.exists(cleanup):
                os.remove(cleanup)
        if os.path.exists(temp_path):
            os.remove(temp_path)


def _resolve_backup_file(backup_path: str) -> str:
    if os.path.isdir(backup_path):
        candidate = os.path.join(backup_path, "latest.sqlite3")
        if os.path.exists(candidate):
            return candidate
        latest = _latest_archive(os.path.join(backup_path, ".history"), candidate_suffix=(".sqlite3.gz",))
        if latest:
            return latest
        return candidate
    if os.path.splitext(backup_path)[1].lower() in _FILE_EXTENSIONS:
        return backup_path
    if backup_path.endswith(".gz"):
        return backup_path
    return backup_path


def _latest_archive(history_dir: str, candidate_suffix: tuple) -> str:
    if not os.path.isdir(history_dir):
        return ""
    candidates = [
        os.path.join(history_dir, name)
        for name in os.listdir(history_dir)
        if name.endswith(candidate_suffix)
    ]
    if not candidates:
        return ""
    return sorted(candidates)[-1]


def list_validation_backups(backup_dir: str) -> List[Dict[str, object]]:
    path = os.path.abspath(backup_dir)
    if os.path.isdir(path):
        payload_paths = []
        latest = os.path.join(path, "latest.sqlite3")
        history = _latest_backups_from_history(os.path.join(path, ".history"))
        if os.path.exists(latest):
            payload_paths.append(latest)
        payload_paths.extend(item["path"] for item in history)
        rows = []
        for item in payload_paths:
            try:
                stat = os.stat(item)
            except FileNotFoundError:
                continue
            if item.endswith(".gz"):
                size = stat.st_size
            else:
                size = stat.st_size
            rows.append(
                {
                    "path": item,
                    "name": os.path.basename(item),
                    "bytes": size,
                    "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                    "mode": "compressed" if item.endswith(".gz") else "single_file",
                }
            )
        return rows

    if os.path.isfile(path):
        stat = os.stat(path)
        return [
            {
                "path": path,
                "name": os.path.basename(path),
                "bytes": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "mode": "single_file",
            }
        ]
    return []


def _latest_backups_from_history(history_dir: str) -> List[Dict[str, object]]:
    items = []
    if not os.path.isdir(history_dir):
        return items
    for name in sorted(os.listdir(history_dir)):
        if not name.endswith(".sqlite3.gz"):
            continue
        path = os.path.join(history_dir, name)
        try:
            stat = os.stat(path)
        except FileNotFoundError:
            continue
        items.append(
            {
                "path": path,
                "name": name,
                "bytes": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "mode": "compressed",
            }
        )
    return items


def prune_validation_backups(backup_dir: str, keep: int = 30) -> List[str]:
    history_dir = os.path.join(os.path.abspath(backup_dir), ".history")
    if not os.path.isdir(history_dir):
        return []
    cutoff = datetime.now() - timedelta(days=max(0, int(keep or 0)))
    removed: List[str] = []
    for name in sorted(os.listdir(history_dir)):
        if not name.endswith(".sqlite3.gz"):
            continue
        path = os.path.join(history_dir, name)
        try:
            stat = os.stat(path)
        except FileNotFoundError:
            continue
        if datetime.fromtimestamp(stat.st_mtime) < cutoff:
            try:
                os.remove(path)
                removed.append(path)
            except Exception:
                continue
    return removed


def _sqlite_backup(source_path: str, target_path: str) -> None:
    with (
        closing(sqlite3.connect(source_path, timeout=30)) as source,
        closing(sqlite3.connect(target_path, timeout=30)) as target,
        source,
        target,
    ):
        source.backup(target)
