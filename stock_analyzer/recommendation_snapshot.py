from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Dict

from .runtime_json import atomic_write_json


def save_recommendation_snapshot(path: str, payload: Dict[str, object]) -> Dict[str, object]:
    from .production_baseline import production_baseline_id

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    snapshot = {
        "schema": 2,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "saved_at_ts": time.time(),
        "production_baseline_id": production_baseline_id(),
        "payload": payload,
    }
    atomic_write_json(path, snapshot, ensure_ascii=False, separators=(",", ":"))
    return {"ok": True, "path": path, "bytes": os.path.getsize(path)}


def load_recommendation_snapshot(
    path: str,
    max_age_seconds: int = 0,
    expected_market: str = "",
    expected_top_n: int = 0,
    expected_baseline_id: str = "",
) -> Dict[str, object]:
    if not os.path.exists(path):
        return {"ok": False, "status": "missing", "path": path}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            snapshot = json.load(handle)
    except Exception as exc:
        return {"ok": False, "status": "invalid", "path": path, "error": str(exc)}
    if not isinstance(snapshot, dict) or snapshot.get("schema") not in {1, 2}:
        return {"ok": False, "status": "unsupported_schema", "path": path}
    if not expected_baseline_id:
        from .production_baseline import production_baseline_id

        expected_baseline_id = production_baseline_id()
    snapshot_baseline_id = str(snapshot.get("production_baseline_id") or "")
    if expected_baseline_id and snapshot_baseline_id != expected_baseline_id:
        return {
            "ok": False,
            "status": "baseline_mismatch",
            "path": path,
            "expected_baseline_id": expected_baseline_id,
            "snapshot_baseline_id": snapshot_baseline_id,
        }
    saved_at_ts = float(snapshot.get("saved_at_ts") or 0.0)
    age_seconds = max(0.0, time.time() - saved_at_ts) if saved_at_ts else None
    if max_age_seconds and age_seconds is not None and age_seconds > max_age_seconds:
        return {
            "ok": False,
            "status": "stale",
            "path": path,
            "age_seconds": round(age_seconds, 2),
            "saved_at": snapshot.get("saved_at", ""),
        }
    payload = snapshot.get("payload")
    if not isinstance(payload, dict):
        return {"ok": False, "status": "invalid_payload", "path": path}
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    if expected_market and str(meta.get("market_filter") or "") != str(expected_market):
        return {
            "ok": False,
            "status": "market_mismatch",
            "path": path,
            "saved_at": snapshot.get("saved_at", ""),
            "age_seconds": round(age_seconds, 2) if age_seconds is not None else None,
            "expected_market": expected_market,
            "snapshot_market": meta.get("market_filter", ""),
        }
    if expected_top_n and int(meta.get("top_n") or 0) != int(expected_top_n):
        return {
            "ok": False,
            "status": "top_n_mismatch",
            "path": path,
            "saved_at": snapshot.get("saved_at", ""),
            "age_seconds": round(age_seconds, 2) if age_seconds is not None else None,
            "expected_top_n": int(expected_top_n),
            "snapshot_top_n": int(meta.get("top_n") or 0),
        }
    return {
        "ok": True,
        "status": "ok",
        "path": path,
        "saved_at": snapshot.get("saved_at", ""),
        "age_seconds": round(age_seconds, 2) if age_seconds is not None else None,
        "payload": payload,
    }
