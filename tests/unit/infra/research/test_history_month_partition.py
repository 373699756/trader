from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from trader.domain.research.baostock_daily import BaoStockDailyCell, BaoStockDailySide
from trader.domain.research.history_monthly import HistoryMonthlyRevision
from trader.infra.research.history_month_partition import (
    HistoryMonthPartitionConflictError,
    HistoryMonthPartitionError,
    SQLiteHistoryMonthPartitionRepository,
)


def _side(day: date, adjustment: str, close: float) -> BaoStockDailySide:
    return BaoStockDailySide(
        "600001",
        day,
        adjustment,  # type: ignore[arg-type]
        close,
        close + 0.5,
        close - 0.5,
        close,
        100.0,
        1000.0,
        close - 0.1 if adjustment == "unadjusted" else None,
        0.01 if adjustment == "unadjusted" else None,
        0.02 if adjustment == "unadjusted" else None,
        "trading",
    )


def _revision(day: date, sequence: int, close: float) -> HistoryMonthlyRevision:
    raw = _side(day, "unadjusted", close)
    qfq = _side(day, "qfq", close - 1.0)
    return HistoryMonthlyRevision(
        sequence,
        "main",
        BaoStockDailyCell("600001", day, "complete", raw, qfq),
        False,
        "bank",
        "sw",
    )


def test_month_partition_schema_revision_replay_and_indexes(tmp_path: Path) -> None:
    path = tmp_path / "partitions/2026/09.sqlite3"
    repository = SQLiteHistoryMonthPartitionRepository(path, 2026, 9)
    repository.initialize()
    original = _revision(date(2026, 9, 10), 1, 10.0)
    revised = _revision(date(2026, 9, 10), 3, 11.0)

    repository.save_revisions((original, original))
    repository.save_revisions((replace(original, first_seen_sequence=2),))
    repository.save_revisions((revised,))

    assert repository.read_day(date(2026, 9, 10), snapshot_sequence=1) == (original,)
    assert repository.read_day(date(2026, 9, 10), snapshot_sequence=2) == (original,)
    assert repository.read_day(date(2026, 9, 10), snapshot_sequence=3) == (revised,)
    assert repository.read_day(date(2026, 9, 10), snapshot_sequence=3, board="star") == ()
    assert repository.read_code("600001", date(2026, 9, 1), date(2026, 9, 30), snapshot_sequence=3) == (revised,)
    with sqlite3.connect(path) as connection:
        indexes = {
            name: tuple(row[2] for row in connection.execute(f"PRAGMA index_info('{name}')"))
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")
        }
    assert indexes["history_month_code_date_idx"][:2] == ("code", "trade_date")
    assert indexes["history_month_date_board_code_idx"][:3] == ("trade_date", "board", "code")
    assert indexes["history_month_observation_code_date_idx"][:3] == (
        "code",
        "trade_date",
        "sync_sequence",
    )

    reverted = _revision(date(2026, 9, 10), 4, 10.0)
    repository.save_revisions((reverted,))
    assert repository.read_day(date(2026, 9, 10), snapshot_sequence=4) == (original,)


def test_month_partition_rejects_backdating_conflicts_and_wrong_month(tmp_path: Path) -> None:
    repository = SQLiteHistoryMonthPartitionRepository(tmp_path / "09.sqlite3", 2026, 9)
    repository.initialize()
    original = _revision(date(2026, 9, 10), 2, 10.0)
    repository.save_revisions((original,))

    with pytest.raises(HistoryMonthPartitionConflictError, match="backdate"):
        repository.save_revisions((replace(original, first_seen_sequence=1),))
    with pytest.raises(HistoryMonthPartitionConflictError, match="sequence"):
        repository.save_revisions((_revision(date(2026, 9, 10), 2, 11.0),))
    with pytest.raises(HistoryMonthPartitionConflictError, match="backdate"):
        repository.save_revisions((_revision(date(2026, 9, 10), 1, 11.0),))
    with pytest.raises(ValueError, match="month"):
        repository.save_revisions((_revision(date(2026, 10, 1), 3, 12.0),))

    empty = SQLiteHistoryMonthPartitionRepository(tmp_path / "ordered.sqlite3", 2026, 9)
    empty.initialize()
    later = _revision(date(2026, 9, 11), 1, 12.0)
    with pytest.raises(ValueError, match="deterministic order"):
        empty.save_revisions((later, _revision(date(2026, 9, 10), 1, 10.0)))
    assert empty.read_day(date(2026, 9, 11), snapshot_sequence=1) == ()


def test_sealed_month_partition_has_stable_hash_and_fails_closed_on_tamper(tmp_path: Path) -> None:
    path = tmp_path / "partitions/2026/09.sqlite3"
    repository = SQLiteHistoryMonthPartitionRepository(path, 2026, 9)
    repository.initialize()
    repository.save_revisions((_revision(date(2026, 9, 10), 1, 10.0),))

    reference = repository.seal()

    assert reference.relative_path == "partitions/2026/09.sqlite3"
    assert reference.row_count == 1
    SQLiteHistoryMonthPartitionRepository.verify(path, reference)
    with path.open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(HistoryMonthPartitionError, match="hash"):
        SQLiteHistoryMonthPartitionRepository.verify(path, reference)


def test_month_partition_refuses_to_seal_invalid_codec_rows(tmp_path: Path) -> None:
    path = tmp_path / "partitions/2026/09.sqlite3"
    repository = SQLiteHistoryMonthPartitionRepository(path, 2026, 9)
    repository.initialize()
    repository.save_revisions((_revision(date(2026, 9, 10), 1, 10.0),))
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE daily_records SET payload_json='{}'")

    with pytest.raises(HistoryMonthPartitionError, match="sealing"):
        repository.seal()

    observation_path = tmp_path / "partitions/2026/observation.sqlite3"
    observation_repository = SQLiteHistoryMonthPartitionRepository(observation_path, 2026, 9)
    observation_repository.initialize()
    observation_repository.save_revisions((_revision(date(2026, 9, 10), 1, 10.0),))
    with sqlite3.connect(observation_path) as connection:
        connection.execute("UPDATE daily_observations SET sync_sequence=2")
    with pytest.raises(HistoryMonthPartitionError, match="observation"):
        observation_repository.seal()
