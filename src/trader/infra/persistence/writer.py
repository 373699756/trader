"""Single-owner publication and staged/committed freeze repository."""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from trader.application.ports.snapshots import RecoverySummary
from trader.domain.outcome.models import (
    BenchmarkReturn,
    OutcomeTarget,
    RecommendationOutcome,
)
from trader.domain.recommendation.models import (
    LiveOverlay,
    RecommendationSnapshot,
    Strategy,
)
from trader.infra.persistence.snapshots import (
    SNAPSHOT_SCHEMA_VERSION,
    snapshot_bytes,
    snapshot_sha256,
)
from trader.infra.persistence.sqlite import connect, connection_scope, initialize_database
from trader.infra.persistence.writer_observability import RepositoryObservabilityMixin
from trader.infra.persistence.writer_utils import (
    SnapshotConflictError,
    _anchor_json,
    _atomic_create_immutable,
    _atomic_replace,
    _manifest_snapshot_error,
    _matches_hash,
    _overlay_from_dict,
    _overlay_to_dict,
    _read_snapshot,
    _verified_manifest_snapshot,
)

FaultInjector = Callable[[str], None]


class SnapshotRepository(RepositoryObservabilityMixin):
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

    def pending_outcome_targets(self, *, limit: int) -> Sequence[OutcomeTarget]:
        if limit < 1:
            raise ValueError("outcome target limit must be positive")
        with connection_scope(self._database_path) as connection:
            rows = connection.execute(
                """
                SELECT r.snapshot_id, r.strategy, r.recommend_date, r.stock_code,
                       r.anchor_price, r.atr20_pct
                FROM recommendations AS r
                JOIN frozen_snapshots AS f ON f.snapshot_id = r.snapshot_id
                WHERE f.status = 'committed' AND r.atr20_pct IS NOT NULL AND r.atr20_pct > 0
                ORDER BY r.recommend_date, r.strategy, r.rank, r.stock_code
                """
            ).fetchall()
            complete = {
                (str(row["snapshot_id"]), str(row["stock_code"]), int(row["horizon"]))
                for row in connection.execute(
                    "SELECT snapshot_id, stock_code, horizon FROM recommendation_outcomes WHERE status = 'complete'"
                ).fetchall()
            }
        targets: list[OutcomeTarget] = []
        for row in rows:
            strategy = Strategy(str(row["strategy"]))
            required = (2, 3, 5) if strategy is Strategy.D25 else (1,)
            identity = (str(row["snapshot_id"]), str(row["stock_code"]))
            if all((*identity, horizon) in complete for horizon in required):
                continue
            targets.append(
                OutcomeTarget(
                    snapshot_id=identity[0],
                    strategy=strategy,
                    recommend_date=str(row["recommend_date"]),
                    stock_code=identity[1],
                    anchor_price=float(row["anchor_price"]),
                    atr20_pct=float(row["atr20_pct"]),
                )
            )
            if len(targets) >= limit:
                break
        return tuple(targets)

    def record_benchmark_return(self, benchmark: BenchmarkReturn, *, observed_at: datetime) -> None:
        with self._lock, connection_scope(self._database_path) as connection:
            connection.execute(
                """
                INSERT INTO outcome_benchmarks(trade_date, return_pct, observed_at)
                VALUES (?, ?, ?)
                ON CONFLICT(trade_date) DO UPDATE SET
                    return_pct = excluded.return_pct,
                    observed_at = excluded.observed_at
                WHERE excluded.observed_at >= outcome_benchmarks.observed_at
                """,
                (benchmark.trade_date, benchmark.return_pct, observed_at.isoformat()),
            )

    def benchmark_returns_after(self, recommend_date: str, *, limit: int) -> Sequence[BenchmarkReturn]:
        if limit < 1:
            return ()
        with connection_scope(self._database_path) as connection:
            rows = connection.execute(
                """
                SELECT trade_date, return_pct
                FROM outcome_benchmarks
                WHERE trade_date > ?
                ORDER BY trade_date
                LIMIT ?
                """,
                (recommend_date, limit),
            ).fetchall()
        return tuple(BenchmarkReturn(str(row["trade_date"]), float(row["return_pct"])) for row in rows)

    def save_recommendation_outcomes(self, outcomes: Sequence[RecommendationOutcome]) -> None:
        if not outcomes:
            return
        with self._lock, connection_scope(self._database_path) as connection:
            connection.executemany(
                """
                INSERT INTO recommendation_outcomes(
                    snapshot_id, strategy, recommend_date, stock_code, horizon, status,
                    settled_at, anchor_price, atr20_pct, minimum_low, end_close,
                    gross_return_pct, benchmark_return_pct, net_excess_return_pct,
                    mae_pct, mae_atr, severe_drawdown, quality_reason, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_id, stock_code, horizon) DO UPDATE SET
                    status = excluded.status,
                    settled_at = excluded.settled_at,
                    minimum_low = excluded.minimum_low,
                    end_close = excluded.end_close,
                    gross_return_pct = excluded.gross_return_pct,
                    benchmark_return_pct = excluded.benchmark_return_pct,
                    net_excess_return_pct = excluded.net_excess_return_pct,
                    mae_pct = excluded.mae_pct,
                    mae_atr = excluded.mae_atr,
                    severe_drawdown = excluded.severe_drawdown,
                    quality_reason = excluded.quality_reason,
                    version = excluded.version
                WHERE recommendation_outcomes.status != 'complete'
                """,
                [
                    (
                        item.snapshot_id,
                        item.strategy.value,
                        item.recommend_date,
                        item.stock_code,
                        item.horizon,
                        item.status,
                        item.settled_at.isoformat(),
                        item.anchor_price,
                        item.atr20_pct,
                        item.minimum_low,
                        item.end_close,
                        item.gross_return_pct,
                        item.benchmark_return_pct,
                        item.net_excess_return_pct,
                        item.mae_pct,
                        item.mae_atr,
                        None if item.severe_drawdown is None else int(item.severe_drawdown),
                        item.quality_reason,
                        item.version,
                    )
                    for item in outcomes
                ],
            )

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

    def recover(self) -> RecoverySummary:
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
        return RecoverySummary(recovered=recovered, quarantined=quarantined, orphaned=orphaned)

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
                        anchor_daily_return_pct, board, board_policy_id, board_rank,
                        board_data_reliability, competition_group_id, selection_skip_reason,
                        merge_epoch, atr20_pct, snapshot_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.strategy.value,
                        snapshot.trade_date,
                        recommendation.features.quote.code,
                        recommendation.rank,
                        price,
                        recommendation.features.quote.pct_change,
                        recommendation.features.quote.board.value,
                        recommendation.features.board_policy_id,
                        recommendation.board_rank,
                        recommendation.features.board_data_reliability,
                        recommendation.features.competition_group_id,
                        recommendation.selection_skip_reason,
                        recommendation.features.merge_epoch,
                        recommendation.features.optional_value("atr20_pct"),
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


__all__ = ["SnapshotConflictError", "SnapshotRepository"]
