from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Dict, List


def backup_validation_db(db_path: str, backup_path: str, label: str = "manual", keep: int = 1) -> Dict[str, object]:
    if not os.path.exists(db_path):
        return {"ok": False, "error": "validation_db_missing", "db_path": db_path}
    os.makedirs(os.path.dirname(backup_path) or ".", exist_ok=True)
    temp_path = f"{backup_path}.tmp"
    try:
        _sqlite_backup(db_path, temp_path)
        os.replace(temp_path, backup_path)
        return {
            "ok": True,
            "path": backup_path,
            "bytes": os.path.getsize(backup_path),
            "mode": "single_file",
            "label": str(label or "manual"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "removed": [],
        }
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def restore_validation_db(backup_path: str, db_path: str, safety_backup_path: str = "", keep: int = 1) -> Dict[str, object]:
    if not os.path.exists(backup_path):
        return {"ok": False, "error": "backup_missing", "backup_path": backup_path}
    safety = None
    if os.path.exists(db_path) and safety_backup_path and os.path.abspath(safety_backup_path) != os.path.abspath(backup_path):
        safety = backup_validation_db(db_path, safety_backup_path, label="pre_restore", keep=keep)
        if not safety.get("ok"):
            return {"ok": False, "error": "pre_restore_backup_failed", "backup": safety}
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    temp_path = f"{db_path}.restore_tmp"
    try:
        _sqlite_backup(backup_path, temp_path)
        os.replace(temp_path, db_path)
        return {
            "ok": True,
            "restored_from": backup_path,
            "db_path": db_path,
            "bytes": os.path.getsize(db_path),
            "pre_restore_backup": safety,
        }
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def list_validation_backups(backup_dir: str) -> List[Dict[str, object]]:
    path = backup_dir
    if os.path.isdir(path):
        path = os.path.join(path, "strategy_validation.backup.sqlite3")
    if not os.path.isfile(path):
        return []
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


def prune_validation_backups(backup_dir: str, keep: int = 30) -> List[str]:
    return []


def _sqlite_backup(source_path: str, target_path: str) -> None:
    with (
        closing(sqlite3.connect(source_path, timeout=30)) as source,
        closing(sqlite3.connect(target_path, timeout=30)) as target,
        source,
        target,
    ):
        source.backup(target)
