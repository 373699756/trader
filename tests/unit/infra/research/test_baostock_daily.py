import json
import sqlite3
from datetime import date, timedelta

import pytest

from trader.domain.research.baostock_daily import (
    BaoStockCalendar,
    BaoStockDailySide,
    BaoStockDailySpec,
    BaoStockSecurity,
    BaoStockSourceVersions,
    join_baostock_daily_sides,
)
from trader.infra.research.baostock_daily import (
    BaoStockDailyArtifactConflictError,
    BaoStockDailyMergedArtifactStore,
    SQLiteBaoStockDailyShard,
)


def _side(code: str, day: date, adjustment: str, close: float = 10.2) -> BaoStockDailySide:
    return BaoStockDailySide(
        code,
        day,
        adjustment,
        10.0,
        10.5,
        9.8,
        close,
        100.0,
        1_000.0,
        9.9 if adjustment == "unadjusted" else None,
        3.03 if adjustment == "unadjusted" else None,
        1.2 if adjustment == "unadjusted" else None,
        "trading",
    )


def _context(count: int = 3):
    spec = BaoStockDailySpec(sessions=count)
    start = spec.source_cutoff - timedelta(days=count - 1)
    calendar = BaoStockCalendar(tuple(start + timedelta(days=index) for index in range(count)))
    universe = (
        BaoStockSecurity("600001", "A", "main", start, None, "fixture"),
        BaoStockSecurity("300001", "B", "chinext", start, None, "fixture"),
    )
    versions = BaoStockSourceVersions("0.9.3", "3.14.0", (("pandas", "2.3.0"),))
    return spec, calendar, universe, versions


def _batch(code: str, calendar: BaoStockCalendar, *, close: float = 10.2):
    dates = calendar.open_dates
    return join_baostock_daily_sides(
        code,
        dates,
        tuple(_side(code, day, "unadjusted", close) for day in dates),
        tuple(_side(code, day, "qfq", close) for day in dates),
    )


def test_sqlite_shard_uses_wal_is_idempotent_and_detects_tampering(tmp_path) -> None:
    spec, calendar, universe, versions = _context()
    shard = SQLiteBaoStockDailyShard(tmp_path / "shard.sqlite3")
    shard.initialize(spec, calendar, universe, versions)
    shard.save_batch(spec, _batch("600001", calendar))
    shard.save_batch(spec, _batch("600001", calendar))

    assert shard.completed_codes(spec) == frozenset({"600001"})
    context = shard.context(spec)
    assert context is not None
    assert context.calendar == calendar
    assert context.universe == tuple(sorted(universe, key=lambda item: item.code))
    assert context.source_versions == versions
    with sqlite3.connect(tmp_path / "shard.sqlite3") as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        connection.execute("UPDATE daily_cells SET payload_json = '{}' WHERE code = '600001'")
    with pytest.raises(BaoStockDailyArtifactConflictError, match="payload"):
        shard.snapshot(spec)


def test_deterministic_merge_is_order_independent_and_manifest_is_hash_bound(tmp_path) -> None:
    spec, calendar, universe, versions = _context()
    first = SQLiteBaoStockDailyShard(tmp_path / "first.sqlite3")
    second = SQLiteBaoStockDailyShard(tmp_path / "second.sqlite3")
    for shard in (first, second):
        shard.initialize(spec, calendar, universe, versions)
    first.save_batch(spec, _batch("600001", calendar))
    second.save_batch(spec, _batch("300001", calendar))

    left = BaoStockDailyMergedArtifactStore(tmp_path / "left").write(
        spec, (first.snapshot(spec), second.snapshot(spec))
    )
    right = BaoStockDailyMergedArtifactStore(tmp_path / "right").write(
        spec, (second.snapshot(spec), first.snapshot(spec))
    )

    assert left.logical_records_hash == right.logical_records_hash
    assert left.audit.content_hash == right.audit.content_hash
    assert left.source_versions == versions
    assert left.source_versions_hash == versions.content_hash
    assert left.production_authority is False
    assert left.point_in_time_parity is False
    assert left.terminal_holdout_opened is False
    descriptor = BaoStockDailyMergedArtifactStore(tmp_path / "left").describe_frozen_daily_input()
    assert descriptor.manifest_hash == left.content_hash
    assert descriptor.source_identity == "score_baostock_daily_core_v2"
    assert descriptor.requested_sessions == 3
    assert descriptor.raw_qfq_layout == "same_row"
    assert {field.name for field in descriptor.fields} >= {"raw_close", "qfq_close", "board"}
    assert (
        BaoStockDailyMergedArtifactStore(tmp_path / "left").write(spec, (first.snapshot(spec), second.snapshot(spec)))
        == left
    )

    manifest_path = tmp_path / "left" / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["logical_records_hash"] = "0" * 64
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BaoStockDailyArtifactConflictError, match="manifest"):
        BaoStockDailyMergedArtifactStore(tmp_path / "left").verify()


def test_shard_rejects_conflicting_content_for_same_code_and_date(tmp_path) -> None:
    spec, calendar, universe, versions = _context()
    shard = SQLiteBaoStockDailyShard(tmp_path / "shard.sqlite3")
    shard.initialize(spec, calendar, universe, versions)
    shard.save_batch(spec, _batch("600001", calendar))

    with pytest.raises(BaoStockDailyArtifactConflictError, match="identity conflict"):
        shard.save_batch(spec, _batch("600001", calendar, close=11.2))


def test_shard_wraps_corrupt_context_and_checkpoint_hash_as_typed_conflicts(tmp_path) -> None:
    spec, calendar, universe, versions = _context()
    path = tmp_path / "shard.sqlite3"
    shard = SQLiteBaoStockDailyShard(path)
    shard.initialize(spec, calendar, universe, versions)
    shard.save_batch(spec, _batch("600001", calendar))

    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE checkpoints SET batch_hash=? WHERE code='600001'", ("0" * 64,))
    with pytest.raises(BaoStockDailyArtifactConflictError, match="checkpoint"):
        shard.snapshot(spec)

    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE context SET calendar_json='{}' WHERE singleton=1")
    with pytest.raises(BaoStockDailyArtifactConflictError, match="context"):
        shard.context(spec)


def test_failed_checkpoint_can_resume_to_completed_without_stale_failure(tmp_path) -> None:
    spec, calendar, universe, versions = _context()
    shard = SQLiteBaoStockDailyShard(tmp_path / "shard.sqlite3")
    shard.initialize(spec, calendar, universe, versions)
    shard.record_failure(spec, "600001", "supplier_query_failed")

    assert shard.snapshot(spec).failures == (("600001", "supplier_query_failed"),)
    shard.save_batch(spec, _batch("600001", calendar))

    assert shard.completed_codes(spec) == frozenset({"600001"})
    assert shard.snapshot(spec).failures == ()


def test_shard_wraps_sqlite_cell_identity_collision_as_typed_conflict(tmp_path) -> None:
    spec, calendar, universe, versions = _context()
    path = tmp_path / "shard.sqlite3"
    shard = SQLiteBaoStockDailyShard(path)
    shard.initialize(spec, calendar, universe, versions)
    with sqlite3.connect(path) as connection:
        cell = _batch("600001", calendar).cells[0]
        connection.execute(
            "INSERT INTO daily_cells VALUES (?, ?, ?, ?)",
            (cell.code, cell.trade_date.isoformat(), "{}", "0" * 64),
        )

    with pytest.raises(BaoStockDailyArtifactConflictError, match="SQLite identity conflict"):
        shard.save_batch(spec, _batch("600001", calendar))
