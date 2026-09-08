import json
import sqlite3
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

import trader.infra.research.baostock_daily as baostock_daily_module
from trader.application.research.baostock_daily import BaoStockShardContext
from trader.domain.research.baostock_daily import (
    BAOSTOCK_CALENDAR_SCHEMA,
    BAOSTOCK_DAILY_FACT_SCHEMA,
    BAOSTOCK_INDUSTRY_INTERVAL_SCHEMA,
    BAOSTOCK_LEGACY_CALENDAR_SCHEMA,
    BAOSTOCK_LEGACY_DAILY_FACT_SCHEMA,
    BAOSTOCK_LEGACY_INDUSTRY_INTERVAL_SCHEMA,
    BaoStockCalendar,
    BaoStockDailyFact,
    BaoStockDailySide,
    BaoStockDailySpec,
    BaoStockIndustryInterval,
    BaoStockSecurity,
    BaoStockSourceVersions,
    join_baostock_daily_sides,
)
from trader.domain.research.h1_point_in_time import canonical_hash
from trader.infra.research.baostock_daily import (
    BaoStockDailyArtifactConflictError,
    BaoStockDailyPartitionedArchive,
    BaoStockTrainingTrainingInputArchive,
    SQLiteBaoStockDailyShard,
)
from trader.infra.research.baostock_daily_serialization import _decode_spec
from trader.infra.research.baostock_partition_archive import _supports_complete_nonproduction_training


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


def test_new_baostock_objects_use_stable_schema_names_and_reject_unknown_versions() -> None:
    spec, calendar, _, _ = _context()
    fact = BaoStockDailyFact("600001", calendar.open_dates[0], False)
    interval = BaoStockIndustryInterval("600001", calendar.open_dates[0], None, "银行", "申万一级行业")

    assert calendar.schema_version == BAOSTOCK_CALENDAR_SCHEMA
    assert fact.schema_version == BAOSTOCK_DAILY_FACT_SCHEMA
    assert interval.schema_version == BAOSTOCK_INDUSTRY_INTERVAL_SCHEMA
    with pytest.raises(ValueError, match="schema"):
        replace(calendar, schema_version="unsupported_calendar_schema")
    with pytest.raises(ValueError, match="payload"):
        replace(fact, schema_version="unsupported_daily_fact_schema")
    with pytest.raises(ValueError, match="interval"):
        replace(interval, schema_version="unsupported_industry_schema")
    assert spec.schema_version == "baostock_daily_core"


def test_legacy_baostock_spec_is_decode_only_and_keeps_its_frozen_hash() -> None:
    raw = {
        "sessions": 2000,
        "research_identity": "score_baostock_daily_core_v2",
        "source_cutoff": "2026-08-31",
        "production_authority": False,
        "point_in_time_parity": False,
        "schema_version": "score_baostock_daily_core_v2",
    }

    decoded = _decode_spec(raw)

    assert decoded.research_identity == raw["research_identity"]
    assert decoded.schema_version == raw["schema_version"]
    assert len(decoded.content_hash) == 64
    with pytest.raises(ValueError, match="identity"):
        BaoStockDailySpec(
            research_identity=str(raw["research_identity"]),
            schema_version=str(raw["schema_version"]),
        )


def test_manifest_descriptor_projects_a_legacy_shard_spec_to_the_stable_identity(tmp_path: Path) -> None:
    spec, calendar, universe, versions = _context()
    universe = (universe[0],)
    shard = SQLiteBaoStockDailyShard(tmp_path / "shards" / "main-6000.sqlite3")
    shard.initialize(spec, calendar, universe, versions)
    shard.save_batch(spec, _batch("600001", calendar))
    legacy_payload = {
        "sessions": spec.sessions,
        "research_identity": "score_baostock_daily_core_v2",
        "source_cutoff": spec.source_cutoff.isoformat(),
        "production_authority": False,
        "point_in_time_parity": False,
        "schema_version": "score_baostock_daily_core_v2",
    }
    legacy_spec = _decode_spec(legacy_payload)
    legacy_context_hash = canonical_hash((legacy_spec, calendar, universe, versions))
    with sqlite3.connect(shard.path) as connection:
        connection.execute(
            "UPDATE context SET spec_json=?, context_hash=? WHERE singleton=1",
            (json.dumps(legacy_payload), legacy_context_hash),
        )

    archive = BaoStockDailyPartitionedArchive(tmp_path)
    manifest = archive.write(spec, (shard,))
    descriptor = archive.describe_frozen_daily_input(manifest)

    assert descriptor.source_identity == "baostock_daily_core"
    assert descriptor.requested_sessions == spec.sessions


def test_sqlite_reads_legacy_calendar_and_training_hashes_without_rewriting_identity(tmp_path) -> None:
    spec, calendar, universe, versions = _context()
    legacy_calendar = replace(calendar, schema_version=BAOSTOCK_LEGACY_CALENDAR_SCHEMA)
    legacy_intervals = tuple(
        replace(item, schema_version=BAOSTOCK_LEGACY_INDUSTRY_INTERVAL_SCHEMA)
        for item in _industry("600001", legacy_calendar)
    )
    legacy_facts = tuple(
        replace(BaoStockDailyFact("600001", day, False), schema_version=BAOSTOCK_LEGACY_DAILY_FACT_SCHEMA)
        for day in legacy_calendar.open_dates
    )
    path = tmp_path / "main-6000.sqlite3"
    shard = SQLiteBaoStockDailyShard(path)
    shard.initialize(spec, legacy_calendar, universe, versions, legacy_intervals)
    shard.save_batch(spec, _batch("600001", legacy_calendar))
    shard.save_training_facts(spec, "600001", legacy_facts, legacy_intervals)

    reopened = SQLiteBaoStockDailyShard(path)
    context = reopened.context(spec)
    assert context is not None
    assert context.calendar.schema_version == BAOSTOCK_LEGACY_CALENDAR_SCHEMA
    assert context.calendar.content_hash == legacy_calendar.content_hash
    assert context.industry_intervals[0].schema_version == BAOSTOCK_LEGACY_INDUSTRY_INTERVAL_SCHEMA
    rows = reopened.read_training_rows(spec, "600001", allowed_dates=frozenset(legacy_calendar.open_dates))
    assert tuple(item.trade_date for item in rows) == legacy_calendar.open_dates

    with sqlite3.connect(path) as connection:
        stored_calendar = json.loads(connection.execute("SELECT calendar_json FROM context").fetchone()[0])
        assert stored_calendar["schema_version"] == BAOSTOCK_LEGACY_CALENDAR_SCHEMA
        stored_fact_hash = connection.execute("SELECT content_hash FROM daily_facts LIMIT 1").fetchone()[0]
    assert stored_fact_hash == legacy_facts[0].content_hash


def test_new_shard_writes_keep_stable_schema_even_after_legacy_read(tmp_path) -> None:
    spec, calendar, universe, versions = _context()
    shard = SQLiteBaoStockDailyShard(tmp_path / "main-6000.sqlite3")
    shard.initialize(spec, calendar, universe, versions)

    with sqlite3.connect(tmp_path / "main-6000.sqlite3") as connection:
        stored_calendar = json.loads(connection.execute("SELECT calendar_json FROM context").fetchone()[0])
    assert stored_calendar["schema_version"] == BAOSTOCK_CALENDAR_SCHEMA


def test_frozen_context_identity_is_reused_across_training_reads(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec, calendar, universe, versions = _context()
    context = BaoStockShardContext(
        calendar,
        tuple(sorted(universe, key=lambda item: item.code)),
        versions,
        _industry("600001", calendar),
    )
    shard = SQLiteBaoStockDailyShard(tmp_path / "main-6000.sqlite3")
    shard.initialize(spec, calendar, universe, versions, context.industry_intervals)
    shard.save_batch(spec, _batch("600001", calendar))
    shard.save_training_facts(spec, "600001", _facts("600001", calendar), context.industry_intervals)
    identity = shard.context_identity(spec, context)
    assert identity is not None

    context_payload = (spec, context.calendar, context.universe, context.source_versions)
    context_hash_calls = 0
    original_hash = baostock_daily_module.canonical_hash

    def tracking_hash(value: object) -> str:
        nonlocal context_hash_calls
        if value == context_payload:
            context_hash_calls += 1
        return original_hash(value)

    monkeypatch.setattr(baostock_daily_module, "canonical_hash", tracking_hash)

    assert shard.context_identity_matches(identity)
    training_identity = shard.training_code_identities(spec, frozen_identity=identity)[0]

    def all_identities_must_not_be_loaded(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("one training row read must validate only its requested code identity")

    monkeypatch.setattr(shard, "training_code_identities", all_identities_must_not_be_loaded)
    assert shard.read_training_rows(
        spec,
        "600001",
        allowed_dates=frozenset(calendar.open_dates),
        frozen_identity=identity,
        expected_identity=training_identity,
    )
    assert context_hash_calls == 0


def test_legacy_hash_compatibility_does_not_accept_tampered_daily_fact_or_industry_hash(tmp_path) -> None:
    spec, calendar, universe, versions = _context()
    legacy_calendar = replace(calendar, schema_version=BAOSTOCK_LEGACY_CALENDAR_SCHEMA)
    legacy_intervals = tuple(
        replace(item, schema_version=BAOSTOCK_LEGACY_INDUSTRY_INTERVAL_SCHEMA)
        for item in _industry("600001", legacy_calendar)
    )
    legacy_facts = tuple(
        replace(BaoStockDailyFact("600001", day, False), schema_version=BAOSTOCK_LEGACY_DAILY_FACT_SCHEMA)
        for day in legacy_calendar.open_dates
    )
    path = tmp_path / "main-6000.sqlite3"
    shard = SQLiteBaoStockDailyShard(path)
    shard.initialize(spec, legacy_calendar, universe, versions, legacy_intervals)
    shard.save_batch(spec, _batch("600001", legacy_calendar))
    shard.save_training_facts(spec, "600001", legacy_facts, legacy_intervals)

    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE daily_facts SET content_hash=?", ("0" * 64,))
    with pytest.raises(BaoStockDailyArtifactConflictError, match="daily fact"):
        shard.read_training_rows(spec, "600001", allowed_dates=frozenset(legacy_calendar.open_dates))

    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE daily_facts SET content_hash=?", (legacy_facts[0].content_hash,))
        connection.execute("UPDATE industry_intervals SET content_hash=?", ("0" * 64,))
    with pytest.raises(BaoStockDailyArtifactConflictError, match="industry interval"):
        shard.read_training_rows(spec, "600001", allowed_dates=frozenset(legacy_calendar.open_dates))


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


def test_checkpoint_index_counts_completed_rows_without_decoding_daily_payload(tmp_path) -> None:
    spec, calendar, universe, versions = _context()
    shard = SQLiteBaoStockDailyShard(tmp_path / "shard.sqlite3")
    shard.initialize(spec, calendar, universe, versions)
    shard.save_batch(spec, _batch("600001", calendar))

    checkpoint = shard.checkpoint(
        spec,
        expected_records_by_code={item.code: len(calendar.expected_dates(item)) for item in universe},
    )

    assert checkpoint.completed_codes == frozenset({"600001"})
    assert checkpoint.ready_codes == frozenset()
    assert checkpoint.failures == ()
    assert checkpoint.downloaded_records == len(calendar.open_dates)


def test_training_facts_keep_valid_later_industry_dates_without_backfilling_early_history(tmp_path) -> None:
    spec, calendar, universe, versions = _context()
    interval = (
        BaoStockIndustryInterval(
            "600001",
            calendar.open_dates[1],
            None,
            "银行",
            "申万一级行业",
        ),
    )
    shard = SQLiteBaoStockDailyShard(tmp_path / "main-6000.sqlite3")
    shard.initialize(spec, calendar, universe, versions, interval)
    shard.save_batch(spec, _batch("600001", calendar))

    shard.save_training_facts(spec, "600001", _facts("600001", calendar)[1:], interval)
    rows = shard.read_training_rows(
        spec,
        "600001",
        allowed_dates=frozenset(calendar.open_dates),
    )

    assert shard.training_ready_codes(spec) == frozenset({"600001"})
    assert tuple(item.trade_date for item in rows) == calendar.open_dates[1:]


def test_partition_manifest_is_streamed_order_independent_hash_bound_and_has_no_merged_database(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    def snapshot_must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("partition sealing must not rebuild every daily domain object")

    for shard in (first, second, first_right, second_right):
        monkeypatch.setattr(shard, "snapshot", snapshot_must_not_run)

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
    assert descriptor.source_identity == "baostock_daily_core"
    assert descriptor.requested_sessions == 3
    assert descriptor.raw_qfq_layout == "same_row"
    assert {field.name for field in descriptor.fields} >= {"raw_close", "qfq_close", "board"}
    assert BaoStockDailyPartitionedArchive(tmp_path / "left").write(spec, (first, second)) == left
    assert len(left.partitions) == 2
    assert {item.relative_path for item in left.partitions} == {
        "shards/main-6000.sqlite3",
        "shards/chinext-3000.sqlite3",
    }
    assert not (tmp_path / "left" / "score-baostock-daily-core.sqlite3").exists()

    manifest_path = tmp_path / "left" / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["logical_records_hash"] = "0" * 64
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BaoStockDailyArtifactConflictError, match="manifest"):
        BaoStockDailyPartitionedArchive(tmp_path / "left").verify()


def test_daily_manifest_rejects_any_post_seal_shard_write(tmp_path: Path) -> None:
    spec, calendar, universe, versions = _context()
    root = tmp_path / "sealed"
    shard = SQLiteBaoStockDailyShard(root / "shards" / "main-6000.sqlite3")
    peer = SQLiteBaoStockDailyShard(root / "shards" / "chinext-3000.sqlite3")
    industry = _industry("600001", calendar)
    industries = industry + _industry("300001", calendar)
    shard.initialize(spec, calendar, universe, versions, industries)
    peer.initialize(spec, calendar, universe, versions, industries)
    shard.save_batch(spec, _batch("600001", calendar))
    peer.save_batch(spec, _batch("300001", calendar))
    archive = BaoStockDailyPartitionedArchive(root)
    archive.write(spec, (shard, peer))

    shard.save_training_facts(spec, "600001", _facts("600001", calendar), industry)

    with pytest.raises(BaoStockDailyArtifactConflictError, match="manifest"):
        archive.verify()


def test_daily_manifest_does_not_require_historical_industry_training_facts(tmp_path: Path) -> None:
    spec, calendar, universe, versions = _context()
    root = tmp_path / "daily-only"
    main = SQLiteBaoStockDailyShard(root / "shards" / "main-6000.sqlite3")
    chinext = SQLiteBaoStockDailyShard(root / "shards" / "chinext-3000.sqlite3")
    for shard in (main, chinext):
        shard.initialize(spec, calendar, universe, versions)
    main.save_batch(spec, _batch("600001", calendar))
    chinext.save_batch(spec, _batch("300001", calendar))

    manifest = BaoStockDailyPartitionedArchive(root).write(spec, (main, chinext))

    assert len(manifest.partitions) == 2
    assert manifest.audit.obtained_cells == manifest.audit.expected_cells
    assert main.training_ready_codes(spec) == frozenset()
    assert chinext.training_ready_codes(spec) == frozenset()


def test_complete_nonproduction_training_tolerates_only_bounded_null_rows(tmp_path: Path) -> None:
    spec, calendar, universe, versions = _context()
    root = tmp_path / "bounded-nulls"
    main = SQLiteBaoStockDailyShard(root / "shards" / "main-6000.sqlite3")
    chinext = SQLiteBaoStockDailyShard(root / "shards" / "chinext-3000.sqlite3")
    for shard in (main, chinext):
        shard.initialize(spec, calendar, universe, versions)
    main.save_batch(spec, _batch("600001", calendar))
    chinext.save_batch(spec, _batch("300001", calendar))
    manifest = BaoStockDailyPartitionedArchive(root).write(spec, (main, chinext))
    bounded_nulls = replace(
        manifest.audit,
        null_rows=1,
        status="historical_data_insufficient",
        failure_reasons=("null_rows_present",),
    )

    assert _supports_complete_nonproduction_training(bounded_nulls)
    assert not _supports_complete_nonproduction_training(
        replace(bounded_nulls, failure_reasons=("null_rows_present", "future_rows_present"))
    )


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


def test_partial_training_input_seals_only_ready_checkpoints_and_remains_stable_after_append(tmp_path) -> None:
    spec, calendar, universe, versions = _context()
    root = tmp_path / "sessions-2000"
    main = SQLiteBaoStockDailyShard(root / "shards" / "main-6000.sqlite3")
    chinext = SQLiteBaoStockDailyShard(root / "shards" / "chinext-3000.sqlite3")
    industries = _industry("600001", calendar) + _industry("300001", calendar)
    for shard in (main, chinext):
        shard.initialize(spec, calendar, universe, versions, industries)
    main.save_batch(spec, _batch("600001", calendar))
    main.save_training_facts(spec, "600001", _facts("600001", calendar), _industry("600001", calendar))

    archive = BaoStockTrainingTrainingInputArchive.open(root, sessions=3, allow_partial_history=True)
    initial_hash = archive.snapshot.content_hash

    assert archive.snapshot.input_scope == "partial_checkpoint"
    assert archive.snapshot.training_codes == ("600001",)
    assert archive.snapshot.completed_code_count == 1
    assert archive.snapshot.universe_count == 2
    assert (
        tuple(
            row.trade_date for row in archive.read_training_rows("600001", allowed_dates=frozenset(calendar.open_dates))
        )
        == calendar.open_dates
    )

    chinext.save_batch(spec, _batch("300001", calendar))
    assert archive.snapshot.content_hash == initial_hash
    assert archive.snapshot.training_codes == ("600001",)


def test_partial_training_input_requires_explicit_authorization_and_rejects_hash_drift(tmp_path) -> None:
    spec, calendar, universe, versions = _context()
    root = tmp_path / "sessions-2000"
    path = root / "shards" / "main-6000.sqlite3"
    shard = SQLiteBaoStockDailyShard(path)
    industry = _industry("600001", calendar)
    shard.initialize(spec, calendar, universe, versions, industry)
    shard.save_batch(spec, _batch("600001", calendar))
    shard.save_training_facts(spec, "600001", _facts("600001", calendar), industry)

    with pytest.raises(BaoStockDailyArtifactConflictError, match="manifest"):
        BaoStockTrainingTrainingInputArchive.open(root, sessions=3, allow_partial_history=False)

    archive = BaoStockTrainingTrainingInputArchive.open(root, sessions=3, allow_partial_history=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE training_fact_checkpoints SET content_hash=? WHERE code='600001'",
            ("0" * 64,),
        )

    with pytest.raises(BaoStockDailyArtifactConflictError, match="snapshot identity"):
        archive.read_training_rows("600001", allowed_dates=frozenset(calendar.open_dates))


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


def test_run_level_worker_failure_checkpoint_can_be_cleared_by_reason(tmp_path) -> None:
    spec, calendar, universe, versions = _context()
    shard = SQLiteBaoStockDailyShard(tmp_path / "shard.sqlite3")
    shard.initialize(spec, calendar, universe, versions)
    shard.record_failure(spec, "600001", "worker_unavailable")

    shard.clear_failures_by_reason(spec, "worker_unavailable")

    assert shard.checkpoint(spec).failures == ()


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
