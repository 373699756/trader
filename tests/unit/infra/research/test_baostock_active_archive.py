from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from trader.domain.research.baostock_active_archive import (
    BAOSTOCK_ARCHIVE_FIELD_FAMILIES,
    BaoStockActiveArchiveContext,
    BaoStockArchiveRecordKey,
    BaoStockFieldCoverage,
)
from trader.domain.research.h1_point_in_time import canonical_hash
from trader.infra.research.baostock_active_archive import (
    BaoStockActiveArchive,
    BaoStockActiveArchiveConflictError,
    BaoStockActiveArchiveView,
    BaoStockIncrementRecord,
)


def _context(root: Path) -> BaoStockActiveArchiveContext:
    calendar_dates = (date(2026, 9, 7), date(2026, 9, 8))
    parent = root / "manifest.json"
    shard = root / "shards" / "main-00.sqlite3"
    shard.parent.mkdir(exist_ok=True)
    with sqlite3.connect(shard) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS daily_cells(code TEXT, trade_date TEXT, payload_json TEXT, content_hash TEXT)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS daily_facts(code TEXT, trade_date TEXT, is_st INTEGER, content_hash TEXT)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS industry_intervals(code TEXT, effective_from TEXT, effective_to TEXT, "
            "industry TEXT, classification TEXT, content_hash TEXT)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS context(singleton INTEGER PRIMARY KEY, spec_json TEXT, calendar_json TEXT)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO context VALUES (1, ?, ?)",
            (
                json.dumps({"sessions": 2}),
                json.dumps({"open_dates": [item.isoformat() for item in calendar_dates]}),
            ),
        )
    if not parent.exists():
        parent.write_text(
            json.dumps(
                {
                    "content_hash": "a" * 64,
                    "partitions": [{"relative_path": "shards/main-00.sqlite3", "codes": ["600001"]}],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return BaoStockActiveArchiveContext(
        parent_manifest_hash="a" * 64,
        parent_manifest_file_hash=hashlib.sha256(parent.read_bytes()).hexdigest(),
        source_cutoff=date(2026, 9, 8),
        calendar_hash=canonical_hash(calendar_dates),
        source_identity_hash="d" * 64,
        partition_by_code=(("600001", "main-00"),),
        active_calendar_dates=calendar_dates,
    )


def _coverage(incremental_rows: int = 1) -> tuple[BaoStockFieldCoverage, ...]:
    return tuple(
        BaoStockFieldCoverage(
            family,
            reusable_rows=1 if family == "daily_raw" else 0,
            incremental_rows=incremental_rows if family == "daily_raw" else 0,
            missing_rows=1 if family in {"qualification", "hard_filter", "risk_facts"} else 0,
            missing_reason=(
                "historical_source_unavailable" if family in {"qualification", "hard_filter", "risk_facts"} else None
            ),
        )
        for family in BAOSTOCK_ARCHIVE_FIELD_FAMILIES
    )


def _record(value: int = 1) -> BaoStockIncrementRecord:
    payload = json.dumps(
        {
            "code": "600001",
            "trade_date": "2026-09-08",
            "adjustment": "unadjusted",
            "open_price": float(value),
            "high_price": float(value),
            "low_price": float(value),
            "close_price": float(value),
            "volume": 1.0,
            "amount": 1.0,
            "preclose": 1.0,
            "pct_change": 0.0,
            "turnover": 0.0,
            "trading_status": "trading",
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return BaoStockIncrementRecord(
        BaoStockArchiveRecordKey("600001", date(2026, 9, 8), "daily_raw"),
        payload,
    )


def _as_legacy_context(payload: dict[str, object]) -> dict[str, object]:
    payload.pop("active_calendar_dates")
    payload["content_hash"] = canonical_hash(
        {
            key: payload[key]
            for key in (
                "parent_manifest_hash",
                "parent_manifest_file_hash",
                "source_cutoff",
                "calendar_hash",
                "source_identity_hash",
                "partition_by_code",
            )
        }
    )
    return payload


def test_publish_is_idempotent_and_parent_files_remain_byte_identical(tmp_path: Path) -> None:
    context = _context(tmp_path)
    parent = tmp_path / "manifest.json"
    before = (parent.read_bytes(), parent.stat().st_mtime_ns)
    archive = BaoStockActiveArchive(tmp_path, context)
    writer = archive.resume_writer()

    writer.save(_record())
    writer.save(_record())
    active = archive.publish(writer, _coverage())
    reopened = BaoStockActiveArchive.open(tmp_path).verify()

    assert reopened == active
    assert reopened.increment_manifest_hash
    assert before == (parent.read_bytes(), parent.stat().st_mtime_ns)
    assert archive.read_increment_record(_record().key) == _record()


def test_conflicting_record_fails_without_switching_the_previous_active_manifest(tmp_path: Path) -> None:
    archive = BaoStockActiveArchive(tmp_path, _context(tmp_path))
    first_writer = archive.resume_writer()
    first_writer.save(_record())
    first = archive.publish(first_writer, _coverage())

    second_writer = archive.resume_writer()
    with pytest.raises(BaoStockActiveArchiveConflictError, match="record conflict"):
        second_writer.save(_record(2))

    assert archive.verify() == first


def test_failed_checkpoint_can_retry_but_is_never_a_completed_record(tmp_path: Path) -> None:
    archive = BaoStockActiveArchive(tmp_path, _context(tmp_path))
    writer = archive.resume_writer()
    key = _record().key

    writer.record_failure(key, "supplier_query_failed")
    assert writer.checkpoint(key).state == "failed"
    assert writer.record_count == 0
    writer.save(_record())

    assert writer.checkpoint(key).state == "completed"
    assert writer.record_count == 1


def test_interrupted_staging_and_tampered_generation_do_not_mutate_parent(tmp_path: Path) -> None:
    context = _context(tmp_path)
    parent = tmp_path / "manifest.json"
    archive = BaoStockActiveArchive(tmp_path, context)
    writer = archive.resume_writer()
    writer.save(_record())
    active = archive.publish(writer, _coverage())
    parent_before = parent.read_bytes()

    interrupted = archive.resume_writer()
    interrupted.record_failure(
        BaoStockArchiveRecordKey("600001", date(2026, 9, 7), "daily_qfq"),
        "supplier_call_timeout",
    )
    assert archive.verify() == active

    manifest = tmp_path / active.increment_manifest_path
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    shard = manifest.parent / payload["partitions"][0]["relative_path"]
    shard.write_bytes(shard.read_bytes() + b"tampered")
    with pytest.raises(BaoStockActiveArchiveConflictError, match="invalid"):
        archive.verify()
    assert parent.read_bytes() == parent_before


def test_interrupted_staging_can_advance_to_a_later_cutoff_without_losing_checkpoints(tmp_path: Path) -> None:
    initial_context = _context(tmp_path)
    archive = BaoStockActiveArchive(tmp_path, initial_context)
    writer = archive.resume_writer()
    writer.save(_record())
    later = BaoStockActiveArchiveContext(
        initial_context.parent_manifest_hash,
        initial_context.parent_manifest_file_hash,
        date(2026, 9, 9),
        canonical_hash((*initial_context.active_calendar_dates, date(2026, 9, 9))),
        initial_context.source_identity_hash,
        initial_context.partition_by_code,
        (*initial_context.active_calendar_dates, date(2026, 9, 9)),
    )

    resumed = BaoStockActiveArchive(tmp_path, later).resume_writer()

    assert resumed.completed(_record().key)
    assert resumed.context == later


def test_legacy_staging_context_is_read_compatibly_without_changing_the_sealed_parent(tmp_path: Path) -> None:
    context = _context(tmp_path)
    parent = tmp_path / "manifest.json"
    parent_before = parent.read_bytes()
    archive = BaoStockActiveArchive(tmp_path, context)
    archive.resume_writer().save(_record())
    context_path = tmp_path / ".increment-staging" / "context.json"
    payload = _as_legacy_context(json.loads(context_path.read_text(encoding="utf-8")))
    context_path.write_text(json.dumps(payload), encoding="utf-8")

    resumed = archive.resume_writer()

    assert resumed.completed(_record().key)
    assert json.loads(context_path.read_text(encoding="utf-8"))["active_calendar_dates"] == [
        "2026-09-07",
        "2026-09-08",
    ]
    assert parent.read_bytes() == parent_before


def test_legacy_sealed_active_context_reopens_read_only_from_parent_and_increment_dates(tmp_path: Path) -> None:
    context = _context(tmp_path)
    archive = BaoStockActiveArchive(tmp_path, context)
    writer = archive.resume_writer()
    writer.save(_record())
    active = archive.publish(writer, _coverage())
    context_path = (tmp_path / active.increment_manifest_path).parent / "context.json"
    payload = _as_legacy_context(json.loads(context_path.read_text(encoding="utf-8")))
    context_path.write_text(json.dumps(payload), encoding="utf-8")

    reopened = BaoStockActiveArchive.open(tmp_path)

    assert reopened.context.active_calendar_dates == context.active_calendar_dates
    assert reopened.verify() == active


def test_active_archive_exposes_hash_bound_dynamic_calendar_and_training_descriptor(tmp_path: Path) -> None:
    context = _context(tmp_path)
    archive = BaoStockActiveArchive(tmp_path, context)
    writer = archive.resume_writer()
    writer.save(_record())
    active = archive.publish(writer, _coverage())

    descriptor = archive.describe_frozen_daily_input()

    assert archive.active_calendar.open_dates == context.active_calendar_dates
    assert archive.universe_codes == ("600001",)
    assert descriptor.manifest_hash == active.active_data_hash
    assert descriptor.source_cutoff == context.source_cutoff
    assert descriptor.requested_sessions == len(context.active_calendar_dates)


def test_active_view_reads_parent_then_increment_and_rejects_overlap_conflicts(tmp_path: Path) -> None:
    shard = tmp_path / "shards" / "main-00.sqlite3"
    shard.parent.mkdir()
    parent_payload = json.dumps(
        {
            "code": "600001",
            "trade_date": "2026-09-07",
            "status": "complete",
            "unadjusted": {
                "code": "600001",
                "trade_date": "2026-09-07",
                "adjustment": "unadjusted",
                "open_price": 7.0,
                "high_price": 7.0,
                "low_price": 7.0,
                "close_price": 7.0,
                "volume": 1.0,
                "amount": 1.0,
                "preclose": 7.0,
                "pct_change": 0.0,
                "turnover": 0.0,
                "trading_status": "trading",
            },
            "qfq": None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    with sqlite3.connect(shard) as connection:
        connection.execute("CREATE TABLE daily_cells(code TEXT, trade_date TEXT, payload_json TEXT, content_hash TEXT)")
        connection.execute("CREATE TABLE daily_facts(code TEXT, trade_date TEXT, is_st INTEGER, content_hash TEXT)")
        connection.execute(
            "CREATE TABLE industry_intervals(code TEXT, effective_from TEXT, effective_to TEXT, "
            "industry TEXT, classification TEXT, content_hash TEXT)"
        )
        connection.execute(
            "INSERT INTO daily_cells VALUES (?, ?, ?, ?)",
            ("600001", "2026-09-07", parent_payload, "e" * 64),
        )
    archive = BaoStockActiveArchive(tmp_path, _context(tmp_path))
    writer = archive.resume_writer()
    writer.save(_record())
    first_active = archive.publish(writer, _coverage())
    view = BaoStockActiveArchiveView(tmp_path, archive)

    parent_key = BaoStockArchiveRecordKey("600001", date(2026, 9, 7), "daily_raw")
    assert view.read(parent_key) is not None
    assert view.read(_record().key) == _record()

    conflicting = archive.resume_writer()
    overlap = json.dumps(
        {
            "code": "600001",
            "trade_date": "2026-09-07",
            "adjustment": "unadjusted",
            "open_price": 8.0,
            "high_price": 8.0,
            "low_price": 8.0,
            "close_price": 8.0,
            "volume": 1.0,
            "amount": 1.0,
            "preclose": 7.0,
            "pct_change": 0.0,
            "turnover": 0.0,
            "trading_status": "trading",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    conflicting.save(BaoStockIncrementRecord(parent_key, overlap))
    with pytest.raises(BaoStockActiveArchiveConflictError, match="parent/increment"):
        archive.publish(conflicting, _coverage(2))
    assert archive.verify() == first_active
