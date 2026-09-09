from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

from scripts.runtime_diagnostics.history_archive_plan import build_archive_plan


def _archive(root: Path) -> None:
    shards = root / "shards"
    shards.mkdir(parents=True)
    path = shards / "main-6000.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE context (
                singleton INTEGER PRIMARY KEY,
                spec_json TEXT NOT NULL,
                calendar_json TEXT NOT NULL,
                universe_json TEXT NOT NULL,
                versions_json TEXT NOT NULL,
                context_hash TEXT NOT NULL
            );
            CREATE TABLE daily_cells (
                code TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                PRIMARY KEY (code, trade_date)
            );
            CREATE TABLE daily_facts (
                code TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                is_st INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                PRIMARY KEY (code, trade_date)
            );
            CREATE TABLE industry_intervals (
                code TEXT NOT NULL,
                effective_from TEXT NOT NULL,
                effective_to TEXT,
                industry TEXT NOT NULL,
                classification TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                PRIMARY KEY (code, effective_from)
            );
            """
        )
        connection.execute(
            "INSERT INTO context VALUES (1, ?, ?, ?, ?, ?)",
            (
                json.dumps(
                    {
                        "sessions": 2,
                        "research_identity": "baostock_daily_core",
                        "source_cutoff": "2026-08-31",
                        "production_authority": False,
                        "point_in_time_parity": False,
                        "schema_version": "baostock_daily_core",
                    }
                ),
                json.dumps(
                    {
                        "open_dates": ["2026-08-28", "2026-08-31"],
                        "schema_version": "baostock_exchange_calendar",
                    }
                ),
                json.dumps(
                    [
                        {
                            "code": "600001",
                            "name": "fixture",
                            "board": "main",
                            "listed_on": "2020-01-01",
                            "delisted_on": None,
                            "source_version": "fixture",
                        }
                    ]
                ),
                json.dumps(
                    {
                        "sdk_version": "fixture",
                        "python_version": "3.14",
                        "dependency_versions": [],
                    }
                ),
                "0" * 64,
            ),
        )
        complete = {
            "code": "600001",
            "trade_date": "2026-08-28",
            "status": "complete",
            "unadjusted": {"present": True},
            "qfq": {"present": True},
        }
        qfq_missing = {
            "code": "600001",
            "trade_date": "2026-08-31",
            "status": "qfq_missing",
            "unadjusted": {"present": True},
            "qfq": None,
        }
        connection.executemany(
            "INSERT INTO daily_cells VALUES (?, ?, ?, ?)",
            (
                ("600001", "2026-08-28", json.dumps(complete), "1" * 64),
                ("600001", "2026-08-31", json.dumps(qfq_missing), "2" * 64),
            ),
        )
        connection.execute("INSERT INTO daily_facts VALUES (?, ?, ?, ?)", ("600001", "2026-08-28", 0, "3" * 64))
        connection.execute(
            "INSERT INTO industry_intervals VALUES (?, ?, ?, ?, ?, ?)",
            ("600001", "2026-09-01", None, "bank", "fixture", "4" * 64),
        )
    manifest = {
        "schema_version": "baostock_daily_manifest",
        "content_hash": "a" * 64,
        "logical_records_hash": "b" * 64,
        "calendar_hash": "c" * 64,
        "universe_hash": "d" * 64,
        "source_versions_hash": "e" * 64,
        "catalog_sha256": "f" * 64,
        "partitions": [
            {
                "relative_path": "shards/main-6000.sqlite3",
                "database_sha256": "0" * 64,
                "logical_records_hash": "1" * 64,
                "row_count": 2,
                "codes": ["600001"],
            }
        ],
        "audit": {"status": "historical_data_insufficient"},
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_archive_plan_reuses_existing_rows_and_requests_only_missing_families(tmp_path: Path) -> None:
    _archive(tmp_path)
    before = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in tmp_path.rglob("*") if path.is_file()}

    plan = build_archive_plan(
        tmp_path,
        target_open_dates=(date(2026, 8, 28), date(2026, 8, 31), date(2026, 9, 1)),
    )

    stock = plan.stocks[0]
    assert plan.parent_manifest_hash == "a" * 64
    assert plan.parent_source_cutoff == date(2026, 8, 31)
    assert plan.target_source_cutoff == date(2026, 9, 1)
    assert stock.existing_first_date == date(2026, 8, 28)
    assert stock.existing_last_date == date(2026, 8, 31)
    assert stock.reusable_daily_cells == 0
    assert stock.missing_raw_dates == (date(2026, 9, 1),)
    assert stock.missing_qfq_dates == (date(2026, 8, 31), date(2026, 9, 1))
    assert stock.missing_is_st_dates == (date(2026, 8, 31), date(2026, 9, 1))
    assert stock.missing_industry_dates == (date(2026, 8, 31),)
    assert plan.estimated_requests.daily_raw == 1
    assert plan.estimated_requests.daily_qfq == 1
    assert plan.estimated_requests.is_st == 0
    assert plan.field_coverage.qualification.missing_rows == 2
    assert plan.field_coverage.hard_filter.missing_rows == 2
    assert plan.field_coverage.risk_facts.missing_rows == 2
    assert plan.production_authority is False
    assert plan.point_in_time_parity is False
    after = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before


def test_archive_plan_rejects_a_target_calendar_that_does_not_extend_the_parent(tmp_path: Path) -> None:
    _archive(tmp_path)

    try:
        build_archive_plan(tmp_path, target_open_dates=(date(2026, 8, 31),))
    except ValueError as exc:
        assert "parent calendar" in str(exc)
    else:
        raise AssertionError("target calendar must retain every parent date before rolling-window trimming")


def test_archive_plan_requests_a_whole_parent_cell_gap_without_redownloading_neighbors(tmp_path: Path) -> None:
    _archive(tmp_path)
    shard = tmp_path / "shards" / "main-6000.sqlite3"
    with sqlite3.connect(shard) as connection:
        connection.execute(
            "DELETE FROM daily_cells WHERE code=? AND trade_date=?",
            ("600001", "2026-08-31"),
        )

    plan = build_archive_plan(
        tmp_path,
        target_open_dates=(date(2026, 8, 28), date(2026, 8, 31)),
    )

    stock = plan.stocks[0]
    assert stock.missing_raw_dates == (date(2026, 8, 31),)
    assert stock.missing_qfq_dates == (date(2026, 8, 31),)
    assert stock.missing_is_st_dates == (date(2026, 8, 31),)
