from __future__ import annotations

import time
from datetime import datetime
from typing import Dict

from ..runtime_json import atomic_write_json


class DeepSeekCache:
    """JSON file cache used by DeepSeek review paths."""

    def read(self, path: str) -> Dict[str, object]:
        try:
            import json

            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def write(self, path: str, cache: Dict[str, object]) -> None:
        try:
            atomic_write_json(path, cache, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            return

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
