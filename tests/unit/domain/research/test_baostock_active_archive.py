from __future__ import annotations

from datetime import date

import pytest

from trader.domain.research.baostock_active_archive import (
    BAOSTOCK_ARCHIVE_FIELD_FAMILIES,
    BaoStockActiveArchiveContext,
    BaoStockActiveManifest,
    BaoStockArchiveRecordKey,
    BaoStockFieldCoverage,
    BaoStockIncrementCheckpoint,
    BaoStockIncrementManifest,
    BaoStockIncrementPartition,
)
from trader.domain.research.h1_point_in_time import canonical_hash


def _coverage() -> tuple[BaoStockFieldCoverage, ...]:
    return tuple(
        BaoStockFieldCoverage(
            family,
            reusable_rows=2 if family == "daily_raw" else 0,
            incremental_rows=1 if family == "daily_raw" else 0,
            missing_rows=1 if family in {"qualification", "hard_filter", "risk_facts"} else 0,
            missing_reason=(
                "historical_source_unavailable" if family in {"qualification", "hard_filter", "risk_facts"} else None
            ),
        )
        for family in BAOSTOCK_ARCHIVE_FIELD_FAMILIES
    )


def test_active_manifest_binds_parent_increment_cutoff_and_field_coverage() -> None:
    increment = BaoStockIncrementManifest(
        parent_manifest_hash="a" * 64,
        parent_manifest_file_hash="0" * 64,
        source_cutoff=date(2026, 9, 8),
        calendar_hash="b" * 64,
        source_identity_hash="c" * 64,
        partitions=(BaoStockIncrementPartition("shards/increment-main-00.sqlite3", "d" * 64, 3, 3),),
        records_hash="e" * 64,
        checkpoints_hash="f" * 64,
    )
    active = BaoStockActiveManifest(
        parent_manifest_hash=increment.parent_manifest_hash,
        parent_manifest_file_hash=increment.parent_manifest_file_hash,
        increment_manifest_hash=increment.content_hash,
        increment_manifest_path=f"increments/{increment.content_hash}/manifest.json",
        source_cutoff=increment.source_cutoff,
        calendar_hash=increment.calendar_hash,
        source_identity_hash=increment.source_identity_hash,
        field_coverage=_coverage(),
    )

    assert active.active_data_hash
    assert active.production_authority is False
    assert active.point_in_time_parity is False
    assert tuple(item.family for item in active.field_coverage) == BAOSTOCK_ARCHIVE_FIELD_FAMILIES


def test_active_context_binds_the_exact_ordered_calendar_to_its_cutoff_and_hash() -> None:
    calendar = (date(2026, 9, 7), date(2026, 9, 8))
    context = BaoStockActiveArchiveContext(
        "a" * 64,
        "b" * 64,
        calendar[-1],
        canonical_hash(calendar),
        "c" * 64,
        (("600001", "main-00"),),
        calendar,
    )

    assert context.active_calendar_dates == calendar
    assert context.universe_codes == ("600001",)
    with pytest.raises(ValueError, match="calendar"):
        BaoStockActiveArchiveContext(
            context.parent_manifest_hash,
            context.parent_manifest_file_hash,
            context.source_cutoff,
            "d" * 64,
            context.source_identity_hash,
            context.partition_by_code,
            calendar,
        )


def test_record_and_checkpoint_identities_are_typed_and_fail_closed() -> None:
    key = BaoStockArchiveRecordKey("600001", date(2026, 9, 8), "daily_raw")
    completed = BaoStockIncrementCheckpoint(key, "completed", content_hash="a" * 64)
    failed = BaoStockIncrementCheckpoint(key, "failed", error_code="supplier_query_failed")

    assert completed.error_code is None
    assert failed.content_hash is None
    with pytest.raises(ValueError, match="checkpoint"):
        BaoStockIncrementCheckpoint(key, "completed", error_code="supplier_query_failed")
    with pytest.raises(ValueError, match="field coverage"):
        BaoStockFieldCoverage("daily_raw", 0, 0, 1)


def test_manifest_rejects_unsafe_paths_and_mismatched_increment_identity() -> None:
    with pytest.raises(ValueError, match="partition"):
        BaoStockIncrementPartition("../parent.sqlite3", "a" * 64, 0, 0)

    with pytest.raises(ValueError, match="increment"):
        BaoStockActiveManifest(
            parent_manifest_hash="a" * 64,
            parent_manifest_file_hash="0" * 64,
            increment_manifest_hash="b" * 64,
            increment_manifest_path="increments/" + "c" * 64 + "/manifest.json",
            source_cutoff=date(2026, 9, 8),
            calendar_hash="d" * 64,
            source_identity_hash="e" * 64,
            field_coverage=_coverage(),
        )
