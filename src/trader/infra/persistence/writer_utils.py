"""Filesystem, manifest and overlay helpers for snapshot persistence."""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from trader.domain.models import LiveOverlay, LiveQuote, RecommendationSnapshot, Strategy
from trader.infra.persistence.snapshots import (
    SNAPSHOT_SCHEMA_VERSION,
    snapshot_from_dict,
    snapshot_sha256,
)

FaultInjector = Callable[[str], None]


class SnapshotConflictError(RuntimeError):
    pass


def _atomic_replace(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _anchor_json(snapshot: RecommendationSnapshot) -> str:
    anchors = {
        item.features.quote.code: {
            "source": item.features.quote.source,
            "source_time": item.features.quote.source_time.isoformat(),
            "age_seconds": round((snapshot.published_at - item.features.quote.source_time).total_seconds(), 3),
        }
        for item in snapshot.recommendations
    }
    return json.dumps(anchors, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _verified_manifest_snapshot(
    row: Mapping[str, object],
    target: Path,
) -> tuple[RecommendationSnapshot | None, str]:
    if not _matches_hash(target, str(row["sha256"])):
        return None, "missing_or_hash_mismatch"
    try:
        snapshot = _read_snapshot(target)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None, "invalid_snapshot_json"
    error = _manifest_snapshot_error(row, snapshot)
    return (snapshot, "") if not error else (None, error)


def _manifest_snapshot_error(row: Mapping[str, object], snapshot: RecommendationSnapshot) -> str:
    expected = {
        "snapshot_id": snapshot.snapshot_id,
        "strategy": snapshot.strategy.value,
        "recommend_date": snapshot.trade_date,
        "fusion_version": snapshot.fusion_version,
        "strategy_version": snapshot.strategy_version,
        "data_version": snapshot.data_version,
    }
    for field, actual in expected.items():
        if str(row[field]) != actual:
            return f"{field}_mismatch"
    if str(row["frozen_at"]) != snapshot.published_at.isoformat():
        return "frozen_at_mismatch"
    if int(str(row["record_count"])) != len(snapshot.recommendations):
        return "record_count_mismatch"
    expected_path = Path("frozen") / snapshot.strategy.value / snapshot.trade_date / f"{snapshot.snapshot_id}.json"
    if str(row["relative_path"]) != expected_path.as_posix():
        return "relative_path_mismatch"
    if str(row["schema_version"]) != SNAPSHOT_SCHEMA_VERSION:
        return "schema_version_mismatch"
    if snapshot.config_version not in {str(row["config_version"]), "legacy-unrecorded"}:
        return "config_version_mismatch"
    if snapshot.config_version != "legacy-unrecorded" and str(row["anchor_json"]) != _anchor_json(snapshot):
        return "anchor_json_mismatch"
    if not snapshot.frozen:
        return "snapshot_not_frozen"
    return ""


def _overlay_to_dict(overlay: LiveOverlay) -> dict[str, object]:
    return {
        "snapshot_id": overlay.snapshot_id,
        "strategy": overlay.strategy.value,
        "trade_date": overlay.trade_date,
        "version": overlay.version,
        "observed_at": overlay.observed_at.isoformat(),
        "closing": overlay.closing,
        "quotes": {
            code: {
                "code": quote.code,
                "price": quote.price,
                "pct_change": quote.pct_change,
                "source": quote.source,
                "source_time": quote.source_time.isoformat(),
                "received_time": quote.received_time.isoformat(),
                "data_version": quote.data_version,
            }
            for code, quote in overlay.quotes.items()
        },
    }


def _overlay_from_dict(raw: Mapping[str, object]) -> LiveOverlay:
    raw_quotes = raw.get("quotes")
    if not isinstance(raw_quotes, dict):
        raise ValueError("live overlay quotes must be an object")
    quotes: dict[str, LiveQuote] = {}
    for code, value in raw_quotes.items():
        if not isinstance(value, dict):
            raise ValueError("live overlay quote must be an object")
        quote = LiveQuote(
            code=str(value["code"]),
            price=float(value["price"]) if value.get("price") is not None else None,
            pct_change=float(value["pct_change"]) if value.get("pct_change") is not None else None,
            source=str(value["source"]),
            source_time=datetime.fromisoformat(str(value["source_time"])),
            received_time=datetime.fromisoformat(str(value["received_time"])),
            data_version=str(value["data_version"]),
        )
        quotes[str(code)] = quote
    return LiveOverlay(
        snapshot_id=str(raw["snapshot_id"]),
        strategy=Strategy(str(raw["strategy"])),
        trade_date=str(raw["trade_date"]),
        version=str(raw["version"]),
        observed_at=datetime.fromisoformat(str(raw["observed_at"])),
        quotes=quotes,
        closing=bool(raw.get("closing")),
    )


def _atomic_create_immutable(target: Path, payload: bytes, *, expected_sha256: str) -> None:
    if target.exists():
        if _matches_hash(target, expected_sha256):
            return
        raise SnapshotConflictError(f"immutable snapshot path already exists with different content: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary_name, target)
        os.unlink(temporary_name)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _matches_hash(path: Path, expected_sha256: str) -> bool:
    try:
        payload = path.read_bytes()
    except OSError:
        return False
    return snapshot_sha256(payload) == expected_sha256


def _read_snapshot(path: Path) -> RecommendationSnapshot:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("snapshot root must be an object")
    return snapshot_from_dict(raw)


def _non_negative_integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return 0
    return max(0, value)


def _optional_number(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * quantile + 0.5)))
    return round(float(ordered[index]), 2)


def _safe_json_object(value: str) -> Mapping[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_json_text(value: Mapping[str, object]) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return "{}"


def _event_integer(event: Mapping[str, object], key: str, *, default: int | None = None) -> int:
    value = event.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"event {key} must be an integer")
    return value
