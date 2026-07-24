"""Archive recommendation dates outside the active twenty-day window."""

from __future__ import annotations

from pathlib import Path

from trader.domain.recommendation.models import Strategy
from trader.infra.persistence.recommendation_archive import RecommendationArchive
from trader.infra.persistence.sqlite import connection_scope
from trader.infra.persistence.writer_utils import SnapshotConflictError, _matches_hash


def archive_trade_date(
    runtime_dir: Path,
    database_path: Path,
    checkpoint_dir: Path,
    archive: RecommendationArchive,
    trade_date: str,
) -> int:
    with connection_scope(database_path) as connection:
        manifests = connection.execute(
            """
            SELECT * FROM frozen_snapshots
            WHERE recommend_date=? AND status='committed'
            ORDER BY strategy
            """,
            (trade_date,),
        ).fetchall()
    archived = 0
    for manifest in manifests:
        relative_path = str(manifest["relative_path"])
        source = runtime_dir / relative_path
        if not _matches_hash(source, str(manifest["sha256"])):
            raise SnapshotConflictError(f"cannot archive an invalid frozen snapshot: {manifest['snapshot_id']}")
        with connection_scope(database_path) as connection:
            overlay = connection.execute(
                """
                SELECT * FROM live_overlays
                WHERE strategy=? AND recommend_date=? AND snapshot_id=?
                """,
                (
                    manifest["strategy"],
                    manifest["recommend_date"],
                    manifest["snapshot_id"],
                ),
            ).fetchone()
            outcomes = connection.execute(
                """
                SELECT * FROM recommendation_outcomes
                WHERE snapshot_id=? ORDER BY stock_code, horizon
                """,
                (manifest["snapshot_id"],),
            ).fetchall()
            recommendations = connection.execute(
                """
                SELECT * FROM recommendations
                WHERE snapshot_id=? ORDER BY rank, stock_code
                """,
                (manifest["snapshot_id"],),
            ).fetchall()
        archive_relative = archive.store(
            snapshot_row=dict(manifest),
            snapshot_payload=source.read_bytes(),
            overlay_row=dict(overlay) if overlay is not None else None,
            outcome_rows=tuple(dict(row) for row in outcomes),
        )
        complete = {
            (str(row["stock_code"]), int(row["horizon"])) for row in outcomes if str(row["status"]) == "complete"
        }
        strategy = Strategy(str(manifest["strategy"]))
        required = (2, 3, 5) if strategy is Strategy.D25 else (1,)
        pending = tuple(
            (
                manifest["snapshot_id"],
                manifest["strategy"],
                manifest["recommend_date"],
                row["stock_code"],
                row["anchor_price"],
                row["atr20_pct"],
                archive_relative.as_posix(),
            )
            for row in recommendations
            if row["atr20_pct"] is not None
            and float(row["atr20_pct"]) > 0
            and not all((str(row["stock_code"]), horizon) in complete for horizon in required)
        )
        _remove_archived_snapshot(
            database_path,
            snapshot_id=str(manifest["snapshot_id"]),
            trade_date=trade_date,
            pending=pending,
        )
        try:
            source.unlink(missing_ok=True)
        except OSError:
            pass
        archived += 1
    try:
        (checkpoint_dir / trade_date).rmdir()
    except OSError:
        pass
    return archived


def _remove_archived_snapshot(
    database_path: Path,
    *,
    snapshot_id: str,
    trade_date: str,
    pending: tuple[tuple[object, ...], ...],
) -> None:
    with connection_scope(database_path) as connection:
        connection.executemany(
            """
            INSERT OR REPLACE INTO outcome_backlog(
                snapshot_id, strategy, recommend_date, stock_code,
                anchor_price, atr20_pct, archive_relative_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            pending,
        )
        connection.execute("DELETE FROM recommendation_outcomes WHERE snapshot_id=?", (snapshot_id,))
        connection.execute("DELETE FROM recommendations WHERE snapshot_id=?", (snapshot_id,))
        connection.execute("DELETE FROM live_overlays WHERE snapshot_id=?", (snapshot_id,))
        connection.execute("DELETE FROM published_snapshots WHERE snapshot_id=?", (snapshot_id,))
        connection.execute("DELETE FROM frozen_snapshots WHERE snapshot_id=?", (snapshot_id,))
        connection.execute("DELETE FROM freeze_checkpoints WHERE trade_date=?", (trade_date,))


__all__ = ["archive_trade_date"]
