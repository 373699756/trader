import json
import sqlite3
from datetime import date, timedelta

import pytest

from trader.domain.research.baostock_daily import (
    BaoStockCalendar,
    BaoStockDailyFact,
    BaoStockDailySide,
    BaoStockDailySpec,
    BaoStockIndustryInterval,
    BaoStockSecurity,
    BaoStockSourceVersions,
    join_baostock_daily_sides,
)
from trader.infra.research.baostock_daily import (
    BaoStockDailyArtifactConflictError,
    BaoStockDailyPartitionedArchive,
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


def _facts(code: str, calendar: BaoStockCalendar) -> tuple[BaoStockDailyFact, ...]:
    return tuple(BaoStockDailyFact(code, day, False) for day in calendar.open_dates)


def _industry(code: str, calendar: BaoStockCalendar) -> tuple[BaoStockIndustryInterval, ...]:
    return (BaoStockIndustryInterval(code, calendar.open_dates[0], None, "银行", "申万一级行业"),)


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


def test_partition_manifest_is_order_independent_hash_bound_and_has_no_merged_database(tmp_path) -> None:
    spec, calendar, universe, versions = _context()
    industries = _industry("600001", calendar) + _industry("300001", calendar)
    left_root = tmp_path / "left"
    right_root = tmp_path / "right"
    first = SQLiteBaoStockDailyShard(left_root / "shards" / "main-6000.sqlite3")
    second = SQLiteBaoStockDailyShard(left_root / "shards" / "chinext-3000.sqlite3")
    first_right = SQLiteBaoStockDailyShard(right_root / "shards" / "main-6000.sqlite3")
    second_right = SQLiteBaoStockDailyShard(right_root / "shards" / "chinext-3000.sqlite3")
    for shard in (first, second, first_right, second_right):
        shard.initialize(spec, calendar, universe, versions, industries)
    first.save_batch(spec, _batch("600001", calendar))
    second.save_batch(spec, _batch("300001", calendar))
    first.save_training_facts(spec, "600001", _facts("600001", calendar), _industry("600001", calendar))
    second.save_training_facts(spec, "300001", _facts("300001", calendar), _industry("300001", calendar))
    first_right.save_batch(spec, _batch("600001", calendar))
    second_right.save_batch(spec, _batch("300001", calendar))
    first_right.save_training_facts(spec, "600001", _facts("600001", calendar), _industry("600001", calendar))
    second_right.save_training_facts(spec, "300001", _facts("300001", calendar), _industry("300001", calendar))

    left = BaoStockDailyPartitionedArchive(left_root).write(spec, (first, second))
    right = BaoStockDailyPartitionedArchive(right_root).write(spec, (second_right, first_right))

    assert left.logical_records_hash == right.logical_records_hash
    assert left.audit.content_hash == right.audit.content_hash
    assert left.source_versions == versions
    assert left.source_versions_hash == versions.content_hash
    assert left.production_authority is False
    assert left.point_in_time_parity is False
    assert left.terminal_holdout_opened is False
    descriptor = BaoStockDailyPartitionedArchive(tmp_path / "left").describe_frozen_daily_input()
    assert descriptor.manifest_hash == left.content_hash
    assert descriptor.source_identity == "score_baostock_daily_core_v2"
    assert descriptor.requested_sessions == 3
    assert descriptor.raw_qfq_layout == "same_row"
    assert {field.name for field in descriptor.fields} >= {"raw_close", "qfq_close", "board"}
    assert BaoStockDailyPartitionedArchive(tmp_path / "left").write(spec, (first, second)) == left
    assert len(left.partitions) == 2
    assert {item.relative_path for item in left.partitions} == {
        "shards/main-6000.sqlite3",
        "shards/chinext-3000.sqlite3",
    }
    assert not (tmp_path / "left" / "score-baostock-daily-core-v2.sqlite3").exists()

    manifest_path = tmp_path / "left" / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["logical_records_hash"] = "0" * 64
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BaoStockDailyArtifactConflictError, match="manifest"):
        BaoStockDailyPartitionedArchive(tmp_path / "left").verify()


def test_training_facts_are_complete_per_code_and_queryable_without_scanning_other_shards(tmp_path) -> None:
    spec, calendar, universe, versions = _context()
    path = tmp_path / "main-6000.sqlite3"
    shard = SQLiteBaoStockDailyShard(path)
    industry = _industry("600001", calendar)
    shard.initialize(spec, calendar, universe, versions, industry)
    shard.save_batch(spec, _batch("600001", calendar))

    assert shard.training_ready_codes(spec) == frozenset()
    shard.save_training_facts(spec, "600001", _facts("600001", calendar), industry)

    assert shard.training_ready_codes(spec) == frozenset({"600001"})
    rows = shard.read_training_rows(spec, "600001", allowed_dates=frozenset(calendar.open_dates))
    assert tuple(row.trade_date for row in rows) == calendar.open_dates
    assert all(row.industry == "银行" and row.is_st is False for row in rows)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM industry_intervals WHERE code='600001'").fetchone() == (1,)


def test_training_facts_reject_industry_intervals_that_differ_from_the_frozen_context(tmp_path) -> None:
    spec, calendar, universe, versions = _context()
    shard = SQLiteBaoStockDailyShard(tmp_path / "main-6000.sqlite3")
    shard.initialize(spec, calendar, universe, versions, _industry("600001", calendar))
    shard.save_batch(spec, _batch("600001", calendar))
    conflicting = (BaoStockIndustryInterval("600001", calendar.open_dates[0], None, "证券", "申万一级行业"),)

    with pytest.raises(BaoStockDailyArtifactConflictError, match="frozen context"):
        shard.save_training_facts(spec, "600001", _facts("600001", calendar), conflicting)

    assert shard.training_ready_codes(spec) == frozenset()


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


def test_cross_shard_retry_can_clear_a_stale_failure_checkpoint(tmp_path) -> None:
    spec, calendar, universe, versions = _context()
    shard = SQLiteBaoStockDailyShard(tmp_path / "shard.sqlite3")
    shard.initialize(spec, calendar, universe, versions)
    shard.record_failure(spec, "600001", "supplier_query_failed")

    shard.clear_failure(spec, "600001")

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
