"""Single-owner publication and staged/committed freeze repository."""

from __future__ import annotations

import json
import math
import os
import shutil
import sqlite3
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from trader.domain.models import LiveOverlay, LiveQuote, RecommendationSnapshot, Strategy
from trader.infrastructure.persistence.snapshots import (
    SNAPSHOT_SCHEMA_VERSION,
    snapshot_bytes,
    snapshot_from_dict,
    snapshot_sha256,
)
from trader.infrastructure.persistence.sqlite import connect, connection_scope, initialize_database

FaultInjector = Callable[[str], None]


class SnapshotConflictError(RuntimeError):
    pass


class SnapshotRepository:
    def __init__(
        self,
        runtime_dir: Path,
        *,
        config_version: str,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self._runtime_dir = runtime_dir
        self._database_path = runtime_dir / "runtime.sqlite3"
        self._published_dir = runtime_dir / "published"
        self._frozen_dir = runtime_dir / "frozen"
        self._quarantine_dir = runtime_dir / "quarantine"
        self._config_version = config_version
        self._fault_injector = fault_injector or (lambda _stage: None)
        self._lock = threading.Lock()

    def initialize(self) -> None:
        self._published_dir.mkdir(parents=True, exist_ok=True)
        self._frozen_dir.mkdir(parents=True, exist_ok=True)
        self._quarantine_dir.mkdir(parents=True, exist_ok=True)
        initialize_database(self._database_path)

    def publish(self, snapshot: RecommendationSnapshot) -> None:
        payload = snapshot_bytes(snapshot)
        digest = snapshot_sha256(payload)
        relative_path = Path("published") / f"{snapshot.strategy.value}.json"
        target = self._runtime_dir / relative_path
        with self._lock:
            _atomic_replace(target, payload)
            self._fault_injector("published_file_replaced")
            with connection_scope(self._database_path) as connection:
                connection.execute(
                    """
                    INSERT INTO published_snapshots(strategy, snapshot_id, published_at, relative_path, sha256)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(strategy) DO UPDATE SET
                        snapshot_id = excluded.snapshot_id,
                        published_at = excluded.published_at,
                        relative_path = excluded.relative_path,
                        sha256 = excluded.sha256
                    """,
                    (
                        snapshot.strategy.value,
                        snapshot.snapshot_id,
                        snapshot.published_at.isoformat(),
                        relative_path.as_posix(),
                        digest,
                    ),
                )

    def freeze(self, snapshot: RecommendationSnapshot) -> None:
        if snapshot.strategy is Strategy.LONG:
            raise ValueError("long watch snapshots are never frozen")
        if snapshot.config_version and snapshot.config_version != self._config_version:
            raise ValueError("snapshot config version does not match repository config version")
        frozen = replace(snapshot, frozen=True, config_version=self._config_version)
        payload = snapshot_bytes(frozen)
        digest = snapshot_sha256(payload)
        relative_path = Path("frozen") / frozen.strategy.value / frozen.trade_date / f"{frozen.snapshot_id}.json"
        target = self._runtime_dir / relative_path
        with self._lock:
            self._stage_manifest(frozen, relative_path, digest)
            self._fault_injector("manifest_staged")
            _atomic_create_immutable(target, payload, expected_sha256=digest)
            self._fault_injector("frozen_file_created")
            self._commit_manifest(frozen)
            self._fault_injector("manifest_committed")

    def latest(self, strategy: Strategy) -> RecommendationSnapshot | None:
        with connection_scope(self._database_path) as connection:
            row = connection.execute(
                "SELECT relative_path, sha256 FROM published_snapshots WHERE strategy = ?",
                (strategy.value,),
            ).fetchone()
        if row is None:
            return None
        return self._load_verified_snapshot(str(row["relative_path"]), str(row["sha256"]))

    def load_frozen(self, strategy: Strategy, trade_date: str) -> RecommendationSnapshot | None:
        with connection_scope(self._database_path) as connection:
            row = connection.execute(
                """
                SELECT relative_path, sha256
                FROM frozen_snapshots
                WHERE strategy = ? AND recommend_date = ? AND status = 'committed'
                """,
                (strategy.value, trade_date),
            ).fetchone()
        if row is None:
            return None
        return self._load_verified_snapshot(str(row["relative_path"]), str(row["sha256"]))

    def recommendation_dates(self, strategy: Strategy) -> Sequence[str]:
        if strategy is Strategy.LONG:
            return ()
        with connection_scope(self._database_path) as connection:
            rows = connection.execute(
                """
                SELECT recommend_date
                FROM frozen_snapshots
                WHERE strategy = ? AND status = 'committed'
                ORDER BY recommend_date DESC
                """,
                (strategy.value,),
            ).fetchall()
        return tuple(str(row["recommend_date"]) for row in rows)

    def save_live_overlay(self, overlay: LiveOverlay) -> bool:
        payload = json.dumps(
            _overlay_to_dict(overlay),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with self._lock, connection_scope(self._database_path) as connection:
            manifest = connection.execute(
                "SELECT snapshot_id FROM frozen_snapshots WHERE strategy = ? AND recommend_date = ? AND status = 'committed'",
                (overlay.strategy.value, overlay.trade_date),
            ).fetchone()
            published = connection.execute(
                "SELECT snapshot_id FROM published_snapshots WHERE strategy = ?",
                (overlay.strategy.value,),
            ).fetchone()
            authorized = (manifest is not None and str(manifest["snapshot_id"]) == overlay.snapshot_id) or (
                published is not None and str(published["snapshot_id"]) == overlay.snapshot_id
            )
            if not authorized:
                raise SnapshotConflictError("live overlay must reference the current published or committed snapshot")
            existing = connection.execute(
                "SELECT snapshot_id, observed_at, closing FROM live_overlays WHERE strategy = ? AND recommend_date = ?",
                (overlay.strategy.value, overlay.trade_date),
            ).fetchone()
            if existing is not None:
                same_snapshot = str(existing["snapshot_id"]) == overlay.snapshot_id
                if bool(existing["closing"]) or (
                    same_snapshot and datetime.fromisoformat(str(existing["observed_at"])) >= overlay.observed_at
                ):
                    return False
            connection.execute(
                """
                INSERT INTO live_overlays(
                    strategy, recommend_date, snapshot_id, version, observed_at, closing, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy, recommend_date) DO UPDATE SET
                    snapshot_id = excluded.snapshot_id,
                    version = excluded.version,
                    observed_at = excluded.observed_at,
                    closing = excluded.closing,
                    payload_json = excluded.payload_json
                """,
                (
                    overlay.strategy.value,
                    overlay.trade_date,
                    overlay.snapshot_id,
                    overlay.version,
                    overlay.observed_at.isoformat(),
                    int(overlay.closing),
                    payload,
                ),
            )
        return True

    def load_live_overlay(self, strategy: Strategy, trade_date: str) -> LiveOverlay | None:
        with connection_scope(self._database_path) as connection:
            row = connection.execute(
                "SELECT * FROM live_overlays WHERE strategy = ? AND recommend_date = ?",
                (strategy.value, trade_date),
            ).fetchone()
        if row is None:
            return None
        try:
            return _overlay_from_dict(json.loads(str(row["payload_json"])))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def recover(self) -> Mapping[str, int]:
        recovered = 0
        quarantined = 0
        with self._lock, connection_scope(self._database_path) as connection:
            staged = connection.execute(
                "SELECT * FROM frozen_snapshots WHERE status = 'staged' ORDER BY frozen_at"
            ).fetchall()
            for row in staged:
                target = self._runtime_dir / str(row["relative_path"])
                snapshot, error = _verified_manifest_snapshot(row, target)
                if snapshot is not None:
                    self._commit_manifest(snapshot, connection=connection)
                    recovered += 1
                else:
                    self._quarantine_manifest(connection, row, target, error)
                    quarantined += 1
            committed = connection.execute(
                "SELECT * FROM frozen_snapshots WHERE status = 'committed' ORDER BY frozen_at"
            ).fetchall()
            for row in committed:
                target = self._runtime_dir / str(row["relative_path"])
                snapshot, error = _verified_manifest_snapshot(row, target)
                if snapshot is None:
                    self._quarantine_manifest(connection, row, target, error)
                    quarantined += 1
            self._restore_invalid_published_pointers(connection)
            known_paths = {
                str(row["relative_path"])
                for row in connection.execute("SELECT relative_path FROM frozen_snapshots").fetchall()
            }
            orphaned = self._quarantine_orphans(known_paths)
        return {"recovered": recovered, "quarantined": quarantined, "orphaned": orphaned}

    def reserve_event(self, event: Mapping[str, object]) -> bool:
        with self._lock, connection_scope(self._database_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO pipeline_events(
                    event_id, event_type, subject_key, trade_date, phase, strategy,
                    priority, data_version, config_version, status, created_at,
                    deadline, retry_count, payload_json, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    str(event["event_id"]),
                    str(event["event_type"]),
                    str(event["subject_key"]),
                    str(event["trade_date"]),
                    str(event["phase"]),
                    str(event["strategy"]),
                    _event_integer(event, "priority"),
                    str(event["data_version"]),
                    str(event["config_version"]),
                    str(event["status"]),
                    str(event["created_at"]),
                    str(event.get("deadline") or ""),
                    _event_integer(event, "retry_count", default=0),
                    json.dumps(event.get("payload") or {}, ensure_ascii=False, separators=(",", ":")),
                    str(event.get("error") or "")[:1000],
                ),
            )
            return cursor.rowcount == 1

    def compare_and_set_event(
        self,
        event_id: str,
        *,
        expected_status: str,
        status: str,
        retry_count: int,
        error: str = "",
    ) -> bool:
        with self._lock, connection_scope(self._database_path) as connection:
            cursor = connection.execute(
                """
                UPDATE pipeline_events
                SET status = ?, retry_count = ?, error = ?
                WHERE event_id = ? AND status = ?
                """,
                (status, retry_count, error[:1000], event_id, expected_status),
            )
            return cursor.rowcount == 1

    def list_events(self, *, cursor: int, limit: int) -> Sequence[Mapping[str, object]]:
        with connection_scope(self._database_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM pipeline_events
                WHERE sequence > ?
                ORDER BY sequence
                LIMIT ?
                """,
                (max(0, cursor), max(1, limit)),
            ).fetchall()
        return tuple(
            {
                **dict(row),
                "payload": json.loads(str(row["payload_json"])),
            }
            for row in rows
        )

    def pending_priority_events(self) -> Sequence[Mapping[str, object]]:
        with connection_scope(self._database_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM pipeline_events
                WHERE status IN ('pending', 'running') AND priority <= 10
                ORDER BY sequence
                """
            ).fetchall()
        return tuple(
            {
                **dict(row),
                "payload": json.loads(str(row["payload_json"])),
            }
            for row in rows
        )

    def record_data_source_health(self, health: Mapping[str, object], *, updated_at: datetime) -> None:
        sources = health.get("sources")
        if not isinstance(sources, Mapping):
            return
        active_source = str(health.get("active_source") or "")
        route = health.get("route")
        if isinstance(route, Mapping):
            route_json = _safe_json_text(route)
            route_status = str(route.get("status") or "idle")
            route_fallback_reason = str(route.get("fallback_reason") or "")
            route_degraded = int(bool(route.get("degraded")))
        else:
            route_json = "{}"
            route_status = "idle"
            route_fallback_reason = ""
            route_degraded = 0
        market_age_summary = health.get("market_quote_age")
        market_age = (
            _optional_number(market_age_summary.get("maximum_seconds"))
            if isinstance(market_age_summary, Mapping)
            else None
        )
        candidate_age_summary = health.get("candidate_quote_age")
        candidate_age = (
            _optional_number(candidate_age_summary.get("maximum_seconds"))
            if isinstance(candidate_age_summary, Mapping)
            else None
        )
        with self._lock, connection_scope(self._database_path) as connection:
            for source, raw in sources.items():
                if not isinstance(raw, Mapping):
                    continue
                connection.execute(
                    """
                    INSERT INTO data_source_health(
                        source, planned_count, success_count, failure_count, circuit_open,
                        p50_latency_ms, p95_latency_ms, data_age_seconds, last_error, route_json,
                        route_status, route_fallback_reason, route_degraded, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source) DO UPDATE SET
                        planned_count = excluded.planned_count,
                        success_count = excluded.success_count,
                        failure_count = excluded.failure_count,
                        circuit_open = excluded.circuit_open,
                        p50_latency_ms = excluded.p50_latency_ms,
                        p95_latency_ms = excluded.p95_latency_ms,
                        data_age_seconds = excluded.data_age_seconds,
                        last_error = excluded.last_error,
                        route_json = excluded.route_json,
                        route_status = excluded.route_status,
                        route_fallback_reason = excluded.route_fallback_reason,
                        route_degraded = excluded.route_degraded,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(source)[:80],
                        _non_negative_integer(raw.get("planned_count")),
                        _non_negative_integer(raw.get("success_count")),
                        _non_negative_integer(raw.get("error_count")),
                        int(bool(raw.get("circuit_open"))),
                        _optional_number(raw.get("p50_latency_ms")),
                        _optional_number(raw.get("p95_latency_ms")),
                        candidate_age
                        if str(source) == "tencent"
                        else market_age
                        if str(source) == active_source
                        else None,
                        str(raw.get("last_error") or "")[:240],
                        route_json,
                        route_status,
                        route_fallback_reason,
                        route_degraded,
                        updated_at.isoformat(),
                    ),
                )

    def observability_status(self) -> Mapping[str, object]:
        try:
            with connection_scope(self._database_path) as connection:
                source_rows = connection.execute("SELECT * FROM data_source_health ORDER BY source").fetchall()
                call_rows = connection.execute(
                    """
                    SELECT outcome, http_status, error_code, latency_ms
                    FROM deepseek_calls ORDER BY requested_at DESC LIMIT 512
                    """
                ).fetchall()
                freeze_rows = connection.execute(
                    """
                    SELECT strategy, recommend_date, frozen_at, data_version, fusion_version,
                           sha256, anchor_json
                    FROM (
                        SELECT strategy, recommend_date, frozen_at, data_version, fusion_version,
                               sha256, anchor_json,
                               ROW_NUMBER() OVER (
                                   PARTITION BY strategy
                                   ORDER BY recommend_date DESC, frozen_at DESC
                               ) AS position
                        FROM frozen_snapshots
                        WHERE status = 'committed'
                    )
                    WHERE position = 1
                    ORDER BY strategy
                    """
                ).fetchall()
        except sqlite3.OperationalError:
            return {"data_sources": {}, "deepseek_calls": {}, "freezes": {}}
        latest_freezes = {
            str(row["strategy"]): {
                "trade_date": str(row["recommend_date"]),
                "frozen_at": str(row["frozen_at"]),
                "data_version": str(row["data_version"]),
                "fusion_version": str(row["fusion_version"]),
                "sha256": str(row["sha256"]),
                "anchors": _safe_json_object(str(row["anchor_json"])),
            }
            for row in freeze_rows
        }
        latencies = tuple(float(row["latency_ms"]) for row in call_rows if isinstance(row["latency_ms"], (int, float)))
        outcomes: dict[str, int] = {}
        for row in call_rows:
            outcome = str(row["outcome"])
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        return {
            "data_sources": {str(row["source"]): dict(row) for row in source_rows},
            "deepseek_calls": {
                "sample_size": len(call_rows),
                "outcomes": outcomes,
                "http_429_count": sum(row["http_status"] == 429 for row in call_rows),
                "timeout_count": sum(str(row["error_code"]) == "timeout" for row in call_rows),
                "p50_latency_ms": _percentile(latencies, 0.50),
                "p95_latency_ms": _percentile(latencies, 0.95),
            },
            "freezes": latest_freezes,
        }

    def _stage_manifest(
        self,
        snapshot: RecommendationSnapshot,
        relative_path: Path,
        digest: str,
    ) -> None:
        with connection_scope(self._database_path) as connection:
            existing = connection.execute(
                "SELECT snapshot_id, sha256, status FROM frozen_snapshots WHERE strategy = ? AND recommend_date = ?",
                (snapshot.strategy.value, snapshot.trade_date),
            ).fetchone()
            if existing is not None:
                if existing["snapshot_id"] == snapshot.snapshot_id and existing["sha256"] == digest:
                    if existing["status"] == "quarantined":
                        raise SnapshotConflictError(
                            f"{snapshot.strategy.value} {snapshot.trade_date} has a quarantined freeze"
                        )
                    return
                raise SnapshotConflictError(f"{snapshot.strategy.value} {snapshot.trade_date} is already frozen")
            connection.execute(
                """
                INSERT INTO frozen_snapshots(
                    snapshot_id, strategy, recommend_date, frozen_at, fusion_version,
                    strategy_version, config_version, schema_version, data_version, relative_path,
                    sha256, record_count, status, anchor_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'staged', ?)
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.strategy.value,
                    snapshot.trade_date,
                    snapshot.published_at.isoformat(),
                    snapshot.fusion_version,
                    snapshot.strategy_version,
                    self._config_version,
                    SNAPSHOT_SCHEMA_VERSION,
                    snapshot.data_version,
                    relative_path.as_posix(),
                    digest,
                    len(snapshot.recommendations),
                    _anchor_json(snapshot),
                ),
            )

    def _commit_manifest(
        self,
        snapshot: RecommendationSnapshot,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        owns_connection = connection is None
        database = connect(self._database_path) if connection is None else connection
        try:
            manifest = database.execute(
                "SELECT * FROM frozen_snapshots WHERE snapshot_id = ?",
                (snapshot.snapshot_id,),
            ).fetchone()
            if manifest is None:
                raise RuntimeError("frozen manifest is missing")
            manifest_status = str(manifest["status"])
            if manifest_status == "quarantined":
                raise SnapshotConflictError(f"frozen snapshot is quarantined: {snapshot.snapshot_id}")
            manifest_error = _manifest_snapshot_error(manifest, snapshot)
            if manifest_error:
                raise SnapshotConflictError(f"frozen manifest mismatch: {manifest_error}")
            database.execute("DELETE FROM recommendations WHERE snapshot_id = ?", (snapshot.snapshot_id,))
            for recommendation in snapshot.recommendations:
                price = recommendation.features.quote.price
                if price is None or price <= 0:
                    raise ValueError(
                        f"cannot freeze recommendation without anchor price: {recommendation.features.quote.code}"
                    )
                database.execute(
                    """
                    INSERT INTO recommendations(
                        strategy, recommend_date, stock_code, rank, anchor_price,
                        anchor_daily_return_pct, snapshot_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.strategy.value,
                        snapshot.trade_date,
                        recommendation.features.quote.code,
                        recommendation.rank,
                        price,
                        recommendation.features.quote.pct_change,
                        snapshot.snapshot_id,
                    ),
                )
            database.execute(
                """
                INSERT INTO published_snapshots(strategy, snapshot_id, published_at, relative_path, sha256)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(strategy) DO UPDATE SET
                    snapshot_id = excluded.snapshot_id,
                    published_at = excluded.published_at,
                    relative_path = excluded.relative_path,
                    sha256 = excluded.sha256
                """,
                (
                    snapshot.strategy.value,
                    snapshot.snapshot_id,
                    snapshot.published_at.isoformat(),
                    str(manifest["relative_path"]),
                    str(manifest["sha256"]),
                ),
            )
            changed = database.execute(
                "UPDATE frozen_snapshots SET status = 'committed', error = '' WHERE snapshot_id = ? AND status = 'staged'",
                (snapshot.snapshot_id,),
            ).rowcount
            if (manifest_status == "staged" and changed != 1) or (manifest_status == "committed" and changed != 0):
                raise RuntimeError("invalid frozen manifest transition")
            if owns_connection:
                database.commit()
        except Exception:
            if owns_connection:
                database.rollback()
            raise
        finally:
            if owns_connection:
                database.close()

    def _load_verified_snapshot(self, relative_path: str, expected_sha256: str) -> RecommendationSnapshot | None:
        target = self._runtime_dir / relative_path
        if not _matches_hash(target, expected_sha256):
            return None
        return _read_snapshot(target)

    def _quarantine_manifest(
        self,
        connection: sqlite3.Connection,
        row: Mapping[str, object],
        target: Path,
        error: str,
    ) -> None:
        connection.execute(
            "UPDATE frozen_snapshots SET status = 'quarantined', error = ? WHERE snapshot_id = ?",
            (error, row["snapshot_id"]),
        )
        if target.exists():
            relative = Path(str(row["relative_path"]))
            destination = self._quarantine_dir / "manifests" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(destination))

    def _restore_invalid_published_pointers(self, connection: sqlite3.Connection) -> None:
        pointers = connection.execute("SELECT * FROM published_snapshots").fetchall()
        for pointer in pointers:
            target = self._runtime_dir / str(pointer["relative_path"])
            manifest = connection.execute(
                """
                SELECT snapshot_id, frozen_at, relative_path, sha256, status
                FROM frozen_snapshots
                WHERE snapshot_id = ? AND strategy = ?
                """,
                (pointer["snapshot_id"], pointer["strategy"]),
            ).fetchone()
            if manifest is not None and str(manifest["status"]) == "committed":
                if str(pointer["relative_path"]) != str(manifest["relative_path"]) or str(pointer["sha256"]) != str(
                    manifest["sha256"]
                ):
                    connection.execute(
                        """
                        UPDATE published_snapshots
                        SET published_at = ?, relative_path = ?, sha256 = ?
                        WHERE strategy = ?
                        """,
                        (
                            manifest["frozen_at"],
                            manifest["relative_path"],
                            manifest["sha256"],
                            pointer["strategy"],
                        ),
                    )
                continue
            if manifest is None and _matches_hash(target, str(pointer["sha256"])):
                continue
            strategy = str(pointer["strategy"])
            fallback = connection.execute(
                """
                SELECT snapshot_id, frozen_at, relative_path, sha256
                FROM frozen_snapshots
                WHERE strategy = ? AND status = 'committed'
                ORDER BY recommend_date DESC, frozen_at DESC
                LIMIT 1
                """,
                (strategy,),
            ).fetchone()
            if fallback is None:
                connection.execute("DELETE FROM published_snapshots WHERE strategy = ?", (strategy,))
                continue
            connection.execute(
                """
                UPDATE published_snapshots
                SET snapshot_id = ?, published_at = ?, relative_path = ?, sha256 = ?
                WHERE strategy = ?
                """,
                (
                    fallback["snapshot_id"],
                    fallback["frozen_at"],
                    fallback["relative_path"],
                    fallback["sha256"],
                    strategy,
                ),
            )

    def _quarantine_orphans(self, known_paths: set[str]) -> int:
        count = 0
        for path in self._frozen_dir.rglob("*.json"):
            relative = path.relative_to(self._runtime_dir).as_posix()
            if relative in known_paths:
                continue
            destination = self._quarantine_dir / "orphans" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(destination))
            count += 1
        return count


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


__all__ = ["SnapshotConflictError", "SnapshotRepository"]
