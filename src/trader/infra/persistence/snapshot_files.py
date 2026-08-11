"""Filesystem, manifest and overlay primitives for snapshot persistence."""

from __future__ import annotations

import json
import math
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from trader.domain.market.models import LiveQuote
from trader.domain.outcome.models import RecommendationOutcome
from trader.domain.recommendation.models import (
    LiveOverlay,
    RecommendationSnapshot,
    Strategy,
)
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
        _fsync_directory(target.parent)
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
    expected_path = Path("frozen") / snapshot.strategy.value / snapshot.trade_date / f"{snapshot.snapshot_id}.json"
    checks = (
        (str(row["frozen_at"]) != snapshot.published_at.isoformat(), "frozen_at_mismatch"),
        (int(str(row["record_count"])) != len(snapshot.recommendations), "record_count_mismatch"),
        (str(row["relative_path"]) != expected_path.as_posix(), "relative_path_mismatch"),
        (str(row["schema_version"]) != SNAPSHOT_SCHEMA_VERSION, "schema_version_mismatch"),
        (
            snapshot.config_version not in {str(row["config_version"]), "legacy-unrecorded"},
            "config_version_mismatch",
        ),
        (
            snapshot.config_version != "legacy-unrecorded" and str(row["anchor_json"]) != _anchor_json(snapshot),
            "anchor_json_mismatch",
        ),
        (not snapshot.frozen, "snapshot_not_frozen"),
    )
    return next((error for invalid, error in checks if invalid), "")


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


def _atomic_create_immutable(
    target: Path,
    payload: bytes,
    *,
    expected_sha256: str,
    fault_injector: FaultInjector | None = None,
) -> None:
    inject = fault_injector or (lambda _stage: None)
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
        inject("json_temporary_fsynced")
        try:
            os.link(temporary_name, target)
        except FileExistsError as exc:
            if not _matches_hash(target, expected_sha256):
                raise SnapshotConflictError(
                    f"immutable snapshot path already exists with different content: {target}"
                ) from exc
            os.unlink(temporary_name)
            _fsync_directory(target.parent)
            return
        inject("json_created")
        os.unlink(temporary_name)
        _fsync_directory(target.parent)
        inject("directory_fsynced")
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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


def _safe_runtime_path(runtime_dir: Path, relative_path: str) -> Path | None:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    try:
        root = runtime_dir.resolve()
        target = (root / relative).resolve(strict=False)
        target.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return target


def _snapshot_from_recovery(
    row: Mapping[str, object],
    *,
    maximum_payload_bytes: int,
) -> tuple[RecommendationSnapshot | None, str]:
    raw = row["recovery_payload"]
    payload = bytes(raw) if isinstance(raw, (bytes, bytearray, memoryview)) else b""
    if not payload or len(payload) > maximum_payload_bytes:
        return None, "recovery_payload_missing_or_oversized"
    digest = snapshot_sha256(payload)
    if digest != str(row["recovery_sha256"]) or digest != str(row["sha256"]):
        return None, "recovery_payload_hash_mismatch"
    try:
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise ValueError("snapshot payload root must be an object")
        snapshot = snapshot_from_dict(decoded)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None, "recovery_payload_invalid"
    error = _manifest_snapshot_error(row, snapshot)
    return (snapshot, "") if not error else (None, f"recovery_{error}")


def _move_staged_file_to_quarantine(
    quarantine_dir: Path,
    row: Mapping[str, object],
    target: Path,
) -> None:
    if not target.exists():
        return
    relative = Path(str(row["relative_path"]))
    destination = quarantine_dir / "recovery" / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination = destination.with_name(f"{destination.stem}-{row['snapshot_id']}{destination.suffix}")
    source_parent = target.parent
    shutil.move(str(target), str(destination))
    _fsync_directory(source_parent)
    _fsync_directory(destination.parent)


def _recover_checkpoints(
    connection: sqlite3.Connection,
    *,
    runtime_dir: Path,
    quarantine_dir: Path,
    config_version: str,
) -> int:
    quarantined = 0
    rows = connection.execute("SELECT * FROM freeze_checkpoints WHERE status='ready'").fetchall()
    for row in rows:
        committed = connection.execute(
            """
            SELECT 1 FROM frozen_snapshots
            WHERE strategy=? AND recommend_date=? AND status='committed'
            """,
            (row["strategy"], row["trade_date"]),
        ).fetchone()
        if committed is not None:
            connection.execute(
                """
                UPDATE freeze_checkpoints SET status='consumed', consumed_at=boundary_at
                WHERE strategy=? AND trade_date=? AND boundary_at=?
                """,
                (row["strategy"], row["trade_date"], row["boundary_at"]),
            )
            continue
        target = _safe_runtime_path(runtime_dir, str(row["relative_path"]))
        snapshot = None
        if target is not None and _matches_hash(target, str(row["sha256"])):
            try:
                snapshot = _read_snapshot(target)
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                snapshot = None
        try:
            boundary = datetime.fromisoformat(str(row["boundary_at"]))
            observed = datetime.fromisoformat(str(row["observed_at"]))
            age = (boundary - observed).total_seconds()
        except (TypeError, ValueError):
            age = -1.0
        valid = (
            snapshot is not None
            and snapshot.strategy.value == str(row["strategy"])
            and snapshot.trade_date == str(row["trade_date"])
            and snapshot.config_version == config_version
            and 0 <= age <= 30
        )
        if valid:
            continue
        connection.execute(
            """
            UPDATE freeze_checkpoints SET status='quarantined'
            WHERE strategy=? AND trade_date=? AND boundary_at=?
            """,
            (row["strategy"], row["trade_date"], row["boundary_at"]),
        )
        if target is not None and target.exists():
            destination = quarantine_dir / "checkpoints" / Path(str(row["relative_path"]))
            destination.parent.mkdir(parents=True, exist_ok=True)
            source_parent = target.parent
            shutil.move(str(target), str(destination))
            _fsync_directory(source_parent)
            _fsync_directory(destination.parent)
        quarantined += 1
    return quarantined


def _quarantine_snapshot_orphans(
    frozen_dir: Path,
    runtime_dir: Path,
    quarantine_dir: Path,
    known_paths: set[str],
) -> int:
    count = 0
    for path in frozen_dir.rglob("*.json"):
        relative = path.relative_to(runtime_dir).as_posix()
        if relative in known_paths:
            continue
        destination = quarantine_dir / "orphans" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_parent = path.parent
        shutil.move(str(path), str(destination))
        _fsync_directory(source_parent)
        _fsync_directory(destination.parent)
        count += 1
    return count


def _outcome_to_row(item: RecommendationOutcome) -> dict[str, object]:
    return {
        "snapshot_id": item.snapshot_id,
        "strategy": item.strategy.value,
        "recommend_date": item.recommend_date,
        "stock_code": item.stock_code,
        "horizon": item.horizon,
        "status": item.status,
        "settled_at": item.settled_at.isoformat(),
        "anchor_price": item.anchor_price,
        "atr20_pct": item.atr20_pct,
        "minimum_low": item.minimum_low,
        "end_close": item.end_close,
        "gross_return_pct": item.gross_return_pct,
        "benchmark_return_pct": item.benchmark_return_pct,
        "net_excess_return_pct": item.net_excess_return_pct,
        "mae_pct": item.mae_pct,
        "mae_atr": item.mae_atr,
        "severe_drawdown": None if item.severe_drawdown is None else int(item.severe_drawdown),
        "quality_reason": item.quality_reason,
        "version": item.version,
    }


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
