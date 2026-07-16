from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Dict

from ..runtime_json import atomic_write_json


class DeepSeekCache:
    """JSON file cache used by DeepSeek review paths."""

    def read(self, path: str) -> Dict[str, object]:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def write(self, path: str, cache: Dict[str, object]) -> None:
        try:
            with self._exclusive_lock(path):
                atomic_write_json(path, cache, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            return

    def merge(self, path: str, updates: Dict[str, object], max_entries: int | None = None) -> None:
        """Merge cache entries while holding a process-safe lock."""
        if not updates:
            return
        try:
            with self._exclusive_lock(path):
                current = self.read(path)
                current.update(updates)
                if max_entries is not None:
                    limit = max(1, int(max_entries))
                    current = dict(
                        sorted(
                            current.items(),
                            key=lambda item: (
                                float((item[1] or {}).get("cached_at") or (item[1] or {}).get("created_at_ts") or 0.0)
                                if isinstance(item[1], dict)
                                else 0.0
                            ),
                            reverse=True,
                        )[:limit]
                    )
                atomic_write_json(path, current, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            return

    @staticmethod
    @contextmanager
    def _exclusive_lock(path: str):
        lock_path = "{}.lock".format(path)
        os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
        with open(lock_path, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def entry_valid(self, entry: Dict[str, object], ttl_seconds: int, schema_version: int = 1) -> bool:
        if not isinstance(entry, dict):
            return False
        if entry.get("schema") != schema_version:
            return False
        if entry.get("date") != datetime.now().strftime("%Y-%m-%d"):
            return False
        if ttl_seconds <= 0:
            return True
        cached_at = float(entry.get("cached_at") or entry.get("created_at_ts") or 0.0)
        return cached_at > 0 and (time.time() - cached_at) <= ttl_seconds
